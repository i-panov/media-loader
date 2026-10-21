#!/bin/sh
set -eu

STRATEGIES_FILE="${BYEDPI_STRATEGIES_FILE:-/config/byedpi-strategies.conf}"
BASE_PORT="${BYEDPI_BASE_PORT:-1080}"

if [ -r "$STRATEGIES_FILE" ]; then
    has_strategy=0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*) continue ;;
        esac
        has_strategy=1
        break
    done < "$STRATEGIES_FILE"

    if [ "$has_strategy" = 1 ]; then
        pids=""
        index=0
        while IFS= read -r line || [ -n "$line" ]; do
            line=$(printf '%s' "$line" | tr -d '\r')
            case "$line" in
                ''|'#'*) continue ;;
            esac
            name=${line%% *}
            args=${line#"$name"}
            args=${args# }
            port=$((BASE_PORT + index))
            echo "byedpi: strategy '$name' on port $port"
            # Deliberately unquoted: args is a whitespace-separated argument list.
            /bin/ciadpi -p "$port" $args &
            pids="$pids $!"
            index=$((index + 1))
        done < "$STRATEGIES_FILE"

        if [ -n "$pids" ]; then
            trap 'kill $pids 2>/dev/null || true; exit 0' INT TERM
            status=0
            wait -n || status=$?
            echo "byedpi: strategy process exited with status $status; restarting pool"
            kill $pids 2>/dev/null || true
            exit 1
        fi
    fi
fi

exec /bin/ciadpi ${BYEDPI_ARGS:---tlsrec 1+s}
