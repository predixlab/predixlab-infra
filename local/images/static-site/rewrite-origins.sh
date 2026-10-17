#!/bin/sh
# Replaces production origins compiled into a static build with local endpoints.
#   rewrite-origins.sh <output-dir> <rules-file>
# Each rule line is "<production-origin>=<local-origin>". A rule that matches no
# file fails the build so origin changes in the app are noticed immediately.
set -eu

output="$1"
rules="$2"
[ -d "$output" ] || { echo "rewrite-origins: build output '$output' not found" >&2; exit 1; }

escape_pattern() { printf '%s' "$1" | sed 's/[][\.*^$#]/\\&/g'; }
escape_replacement() { printf '%s' "$1" | sed 's/[\&#]/\\&/g'; }

text_files() {
  find "$output" -type f \( -name '*.html' -o -name '*.js' -o -name '*.mjs' -o -name '*.css' \
    -o -name '*.json' -o -name '*.webmanifest' -o -name '*.xml' -o -name '*.txt' \)
}

while IFS= read -r rule || [ -n "$rule" ]; do
  [ -z "$rule" ] && continue
  from="${rule%%=*}"
  to="${rule#*=}"
  pattern=$(escape_pattern "$from")
  replacement=$(escape_replacement "$to")
  count=0
  for file in $(text_files); do
    if grep -qF -e "$from" "$file"; then
      sed -i "s#${pattern}#${replacement}#g" "$file"
      count=$((count + 1))
    fi
  done
  if [ "$count" -eq 0 ]; then
    echo "rewrite-origins: '$from' not found in $output; update originRewrites in config/platform.json" >&2
    exit 1
  fi
  echo "rewrite-origins: $from -> $to in $count file(s)"
done < "$rules"
