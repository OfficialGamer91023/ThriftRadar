#!/bin/sh
# Run on the VM (Ubuntu 24.04, aarch64) as the default user: ~/thriftradar/setup.sh <public-ip>. Idempotent.
# Installs Docker, opens 80/443 in the host firewall (Oracle's Ubuntu images block them), creates the
# secrets file with a random SESSION_SECRET (the Featherless key is added by the user, never through chat),
# then builds and starts the stack.
set -eu
IP="${1:?usage: setup.sh <public-ip>}"
cd "$(dirname "$0")"
if ! command -v docker >/dev/null; then
  sudo apt-get update -q && sudo apt-get install -y -q docker.io docker-compose-v2
fi
for p in 80 443; do
  sudo iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 5 -p tcp --dport $p -j ACCEPT
done
sudo sh -c 'command -v netfilter-persistent >/dev/null && netfilter-persistent save' || true
umask 077
[ -f secrets.env ] || printf 'SESSION_SECRET=%s\n' "$(openssl rand -hex 32)" > secrets.env
grep -q '^FEATHERLESS_API_KEY=.' secrets.env || echo "setup: add FEATHERLESS_API_KEY to secrets.env (see the steps); starting with the AI off" >&2
grep -q '^FEATHERLESS_API_KEY=.' secrets.env || grep -q '^VLM_PROVIDER=' secrets.env || echo 'VLM_PROVIDER=off' >> secrets.env
echo "SITE=$(echo "$IP" | tr . -).sslip.io" > .env
# Build first, start after: compose reads secrets.env when `up` starts, so a key added during a long
# build would otherwise be missed (it happened on the first deploy).
sudo docker compose build
sudo docker compose up -d
echo "setup: started; https://$(echo "$IP" | tr . -).sslip.io (the first build takes ~15 min)"
