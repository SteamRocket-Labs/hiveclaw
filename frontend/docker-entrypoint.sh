#!/bin/sh
# Resolve DNS server from /etc/resolv.conf
# Prefer IPv4; wrap IPv6 in brackets for nginx compatibility
RESOLVER=$(awk '/^nameserver/{
  if ($2 !~ /:/) { print $2; exit }
}' /etc/resolv.conf 2>/dev/null)

if [ -z "$RESOLVER" ]; then
  # No IPv4 found, try IPv6 with brackets
  IPV6=$(awk '/^nameserver/{if ($2 ~ /:/) { print $2; exit }}' /etc/resolv.conf 2>/dev/null)
  if [ -n "$IPV6" ]; then
    RESOLVER="[$IPV6]"
  else
    RESOLVER="8.8.8.8"
  fi
fi

sed -i "s|resolver 127.0.0.11 valid=10s ipv6=off|resolver $RESOLVER valid=10s|g" /etc/nginx/conf.d/default.conf

# Replace backend upstream if BACKEND_HOST is set
if [ -n "$BACKEND_HOST" ]; then
  sed -i "s|backend:8000|$BACKEND_HOST|g" /etc/nginx/conf.d/default.conf
fi

exec nginx -g 'daemon off;'
