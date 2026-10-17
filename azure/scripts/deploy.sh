#!/usr/bin/env bash
# Validate, preview (what-if) or deploy the PredixLab Azure infrastructure.
#
#   azure/scripts/deploy.sh what-if            # default parameter file: parameters/prod.bicepparam
#   azure/scripts/deploy.sh validate
#   azure/scripts/deploy.sh deploy [--yes]
#
# Application releases are owned by each repository's CI. Before calling ARM this
# script reads the live image tags (tagPolicy "live" in config/platform.json) and
# the campaign Function App package URL, so an infrastructure deployment never
# rolls a workload back or detaches its code package.
#
# Nothing specific to a subscription or a person is committed. The target
# subscription is --subscription, AZURE_SUBSCRIPTION_ID or the Azure CLI's current
# subscription, and every deployment parameter in config/platform.json
# ("parameters") is read from PLX_PARAM_<NAME>. Both can be kept in an untracked
# env file next to the parameter file (parameters/prod.env, see prod.env.example).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AZURE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd "$AZURE_DIR/.." && pwd)"
PLATFORM_JSON="$REPO_DIR/config/platform.json"

ACTION="${1:-}"
shift || true

PARAM_FILE="$AZURE_DIR/parameters/prod.bicepparam"
RESOURCE_GROUP="predixlab"
SUBSCRIPTION=""
ASSUME_YES=false
SHOW_UNCHANGED=false

log() { printf '[deploy] %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
  cat <<'EOF'

Options:
  --parameters <file>     .bicepparam file (default: azure/parameters/prod.bicepparam)
  --resource-group <rg>   target resource group (default: predixlab)
  --subscription <id>     target subscription (default: AZURE_SUBSCRIPTION_ID, then the Azure CLI's current subscription)
  --show-unchanged        what-if: include NoChange/Ignore resources
  --yes                   deploy without the interactive what-if confirmation
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --parameters) PARAM_FILE="$2"; shift 2 ;;
    --resource-group) RESOURCE_GROUP="$2"; shift 2 ;;
    --subscription) SUBSCRIPTION="$2"; shift 2 ;;
    --show-unchanged) SHOW_UNCHANGED=true; shift ;;
    --yes) ASSUME_YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$ACTION" in
  validate|what-if|deploy) ;;
  -h|--help|"") usage; exit 0 ;;
  *) echo "Unknown action: $ACTION" >&2; usage >&2; exit 2 ;;
esac

for tool in az jq; do
  command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }
done

# 1. Untracked KEY=VALUE file next to the parameter file. It is parsed (never
#    sourced) and does not override variables that are already set.
ENV_FILE="${PARAM_FILE%.bicepparam}.env"
load_env_file() {
  local file="$1" line key value number=0
  local skip='^[[:space:]]*(#.*)?$'
  local assignment='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
  local double_quoted='^"(.*)"$' single_quoted="^'(.*)'$"
  while IFS= read -r line || [[ -n "$line" ]]; do
    number=$((number + 1))
    line="${line%$'\r'}"
    [[ "$line" =~ $skip ]] && continue
    [[ "$line" =~ $assignment ]] || die "$file:$number: expected KEY=VALUE"
    key="${BASH_REMATCH[2]}"
    value="${BASH_REMATCH[3]}"
    if [[ "$value" =~ $double_quoted || "$value" =~ $single_quoted ]]; then
      value="${BASH_REMATCH[1]}"
    fi
    [[ -n "${!key+x}" ]] || export "$key=$value"
  done < "$file"
}
if [[ -f "$ENV_FILE" ]]; then
  load_env_file "$ENV_FILE"
  log "loaded ${ENV_FILE#"$REPO_DIR"/}"
fi

# 2. Deployment parameters (config/platform.json "parameters").
missing=""
while IFS=$'\t' read -r key description; do
  env_name="PLX_PARAM_$(printf '%s' "$key" | tr '[:lower:]-' '[:upper:]_')"
  if [[ -n "${!env_name:-}" ]]; then
    export "${env_name?}"
  else
    missing+="  $env_name ($key: $description)"$'\n'
  fi
done < <(jq -r '(.parameters // {}) | to_entries[] | [.key, (.value.description // "")] | @tsv' "$PLATFORM_JSON")
if [[ -n "$missing" ]]; then
  printf '[deploy] missing deployment parameters:\n%s' "$missing" >&2
  die "Export them or add them to ${ENV_FILE#"$REPO_DIR"/} (see azure/parameters/prod.env.example)."
fi

# 3. Target subscription; the parameter file builds Key Vault references from it.
if [[ -n "${SUBSCRIPTION:=${AZURE_SUBSCRIPTION_ID:-}}" ]]; then
  account=$(az account show --subscription "$SUBSCRIPTION" -o json 2>/dev/null) \
    || die "Subscription $SUBSCRIPTION is not accessible. Sign in with 'az login'."
else
  account=$(az account show -o json 2>/dev/null) \
    || die "Sign in with 'az login', or choose a subscription with --subscription or AZURE_SUBSCRIPTION_ID."
fi
SUBSCRIPTION=$(jq -r '.id' <<<"$account")
subscription_name=$(jq -r '.name' <<<"$account")
[[ "$(az group exists --subscription "$SUBSCRIPTION" --name "$RESOURCE_GROUP" -o tsv)" == "true" ]] \
  || die "Resource group $RESOURCE_GROUP not found in subscription '$subscription_name'. Choose the subscription with --subscription, AZURE_SUBSCRIPTION_ID or 'az account set'."
export AZURE_SUBSCRIPTION_ID="$SUBSCRIPTION"
log "target: resource group $RESOURCE_GROUP in subscription '$subscription_name'"

# Prints the running image of the first live workload container that uses <image-key>.
live_image() {
  local key="$1" kind name container image
  while IFS=$'\t' read -r kind name container; do
    if [[ "$kind" == "app" ]]; then
      image=$(az containerapp show --subscription "$SUBSCRIPTION" -g "$RESOURCE_GROUP" -n "$name" \
        --query "properties.template.containers[?name=='$container'].image | [0]" -o tsv 2>/dev/null || true)
    else
      image=$(az containerapp job show --subscription "$SUBSCRIPTION" -g "$RESOURCE_GROUP" -n "$name" \
        --query "properties.template.containers[?name=='$container'].image | [0]" -o tsv 2>/dev/null || true)
    fi
    if [[ -n "$image" ]]; then
      printf '%s' "$image"
      return 0
    fi
  done < <(jq -r --arg key "$key" '
      (.containerApps[] | . as $w | .containers[] | select(.image == $key) | ["app", $w.azureName, .name]),
      (.jobs[] | . as $w | .containers[] | select(.image == $key) | ["job", $w.azureName, .name])
      | @tsv' "$PLATFORM_JSON")
  return 1
}

# 4. Live image tags.
while read -r key; do
  env_name="PLX_IMAGE_TAG_$(printf '%s' "$key" | tr '[:lower:]-' '[:upper:]_')"
  if [[ -n "${!env_name:-}" ]]; then
    log "$key: using $env_name=${!env_name} from the environment"
    continue
  fi
  if image=$(live_image "$key"); then
    tag="${image##*:}"
    export "$env_name=$tag"
    log "$key: keeping live tag $tag"
  else
    log "$key: no running workload found, falling back to the default tag"
  fi
done < <(jq -r '.images | to_entries[] | select(.value.tagPolicy == "live") | .key' "$PLATFORM_JSON")

# 5. Campaign package URL (owned by the campaign CI).
if [[ -z "${PLX_CAMPAIGN_RUN_FROM_PACKAGE:-}" ]]; then
  function_app=$(jq -r '.functionApps[0].azureName' "$PLATFORM_JSON")
  PLX_CAMPAIGN_RUN_FROM_PACKAGE=$(az functionapp config appsettings list --subscription "$SUBSCRIPTION" \
    -g "$RESOURCE_GROUP" -n "$function_app" \
    --query "[?name=='WEBSITE_RUN_FROM_PACKAGE'].value | [0]" -o tsv 2>/dev/null || true)
  export PLX_CAMPAIGN_RUN_FROM_PACKAGE
  if [[ -n "$PLX_CAMPAIGN_RUN_FROM_PACKAGE" ]]; then
    log "$function_app: preserving the current WEBSITE_RUN_FROM_PACKAGE"
  else
    log "$function_app: no WEBSITE_RUN_FROM_PACKAGE found (new app or zip deploy)"
  fi
fi

DEPLOYMENT_NAME="predixlab-infra-$(date -u +%Y%m%d%H%M%S)"
COMMON=(--subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" --parameters "$PARAM_FILE")

case "$ACTION" in
  validate)
    az deployment group validate "${COMMON[@]}" --name "$DEPLOYMENT_NAME" --query "properties.provisioningState" -o tsv
    ;;
  what-if)
    if [[ "$SHOW_UNCHANGED" == true ]]; then
      az deployment group what-if "${COMMON[@]}" --name "$DEPLOYMENT_NAME"
    else
      az deployment group what-if "${COMMON[@]}" --name "$DEPLOYMENT_NAME" --exclude-change-types NoChange Ignore
    fi
    ;;
  deploy)
    if [[ "$ASSUME_YES" == true ]]; then
      az deployment group create "${COMMON[@]}" --name "$DEPLOYMENT_NAME" -o none
    else
      az deployment group create "${COMMON[@]}" --name "$DEPLOYMENT_NAME" --confirm-with-what-if -o none
    fi
    log "deployment $DEPLOYMENT_NAME finished"
    ;;
esac
