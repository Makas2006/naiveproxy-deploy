#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/install_debian.sh"
  exit 1
fi

apt-get update
apt-get install -y ca-certificates curl gnupg lsb-release

install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
fi

ARCH="$(dpkg --print-architecture)"
CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
cat >/etc/apt/sources.list.d/docker.list <<EOF
deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${CODENAME} stable
EOF

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

cat >/etc/sysctl.d/99-naiveproxy.conf <<'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
fs.file-max=1048576
EOF
sysctl --system

cat >/etc/security/limits.d/99-naiveproxy.conf <<'EOF'
* soft nofile 1048576
* hard nofile 1048576
root soft nofile 1048576
root hard nofile 1048576
EOF

mkdir -p /etc/systemd/system/docker.service.d
cat >/etc/systemd/system/docker.service.d/99-naiveproxy-nofile.conf <<'EOF'
[Service]
LimitNOFILE=1048576
EOF

systemctl daemon-reload
systemctl restart docker

cd "${PROJECT_DIR}"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created ${PROJECT_DIR}/.env. Edit it before starting the stack."
  exit 0
fi

docker compose up -d --build
docker compose ps
