#!/bin/sh
set -eu

# Deliberately unquoted: BYEDPI_ARGS is a whitespace-separated argument list.
exec /bin/ciadpi ${BYEDPI_ARGS:---auto=torst --timeout 3 --tlsrec 3+s}
