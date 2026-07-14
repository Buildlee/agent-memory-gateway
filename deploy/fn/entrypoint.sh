#!/bin/sh
set -eu

secret_file="${MEMORY_GATEWAY_SECRETS_FILE:-/run/secrets/metadata.env}"
if [ ! -r "$secret_file" ]; then
  echo "缺少只读 Gateway secret 文件" >&2
  exit 78
fi

while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|'#'*) ;;
    *=*)
      key=${line%%=*}
      value=${line#*=}
      case "$key" in
        MEMORY_[A-Z0-9_]*) export "$key=$value" ;;
        *)
          echo "secret 文件包含不允许的变量名" >&2
          exit 78
          ;;
      esac
      ;;
    *)
      echo "secret 文件格式无效" >&2
      exit 78
      ;;
  esac
done < "$secret_file"

exec "$@"
