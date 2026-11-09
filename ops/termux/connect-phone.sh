#!/usr/bin/env bash
# Install the privately provisioned phone bundle, preserving the previous gateway.
set -euo pipefail
umask 077
: "${PREFIX:?Run this script inside Termux on the phone}"
command -v sv >/dev/null || { echo 'termux-services required: pkg install termux-services'; exit 1; }
python_bin=$(command -v python)
"$python_bin" -c 'import websockets' || { echo 'Install dependency: python -m pip install websockets'; exit 1; }
source_dir=$(cd "$(dirname "$0")" && pwd)
[ -f "$source_dir/phone.env" ] || { echo 'Private phone.env is missing from this bundle'; exit 1; }
app_dir="$HOME/.local/share/rhm-selfhost"
config_dir="$HOME/.config/rhm-selfhost"
svdir="$PREFIX/var/service"
old_service="$svdir/rhm-agent"
new_service="$svdir/rhm-selfhost"
backup_dir="$HOME/.local/state/rhm-migration/$(date +%Y%m%d-%H%M%S)"
[ ! -e "$new_service/run" ] || { echo 'rhm-selfhost is already installed; refusing to overwrite it'; exit 1; }
mkdir -p "$app_dir" "$config_dir" "$backup_dir" "$HOME/.termux/boot"
cp -R "$source_dir/core" "$source_dir/skills" "$app_dir/"
"$python_bin" - "$source_dir/phone.env" "$config_dir" "$HOME" <<'PY'
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1]).parent))
from core.domain import read_env_file
out=Path(sys.argv[2]); home=Path(sys.argv[3])
config=read_env_file(sys.argv[1])
for old in (home/'.config/remote-harness-mcp/agent.env', home/'.config/antigravity-mesh/agent.env'):
    if old.exists():
        for k,v in read_env_file(str(old)).items():
            if k.startswith('MESH_DEVICE') or k in {'MESH_READ_ONLY','MESH_WRITE_ROOTS'}:
                config[k]=v
        break
config['MESH_DOMAIN_FILE']=str(out/'domain.env')
config['MESH_WORKSPACE']=str(home)
config['MESH_UPDATE_CHECK']='0'; config['MESH_UPDATE_AUTO']='0'
# Config is sourced by the launcher, so quote every value, including inherited ones.
import shlex
for filename,values in [('agent.env',config),('domain.env',{'MESH_PUBLIC_URL':config['MESH_PUBLIC_URL']})]:
    p=out/filename
    with os.fdopen(os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as f:
        f.write(''.join(k+'='+shlex.quote(v)+'\n' for k,v in values.items()))
PY
cat > "$config_dir/start.sh" <<EOF
#!$PREFIX/bin/bash
export PATH="$PREFIX/bin:/system/bin:\$PATH"
export HOME="$HOME"
set -a
. "$config_dir/agent.env"
set +a
export MESH_CONFIG_FILE="$config_dir/agent.env"
cd "$app_dir"
exec "$python_bin" -m core.agent
EOF
chmod 700 "$config_dir/start.sh"
# The old service/config stay in place. Record whether it was enabled for rollback.
if [ -d "$old_service" ]; then
    cp -R "$old_service" "$backup_dir/rhm-agent-service"
    [ -e "$old_service/down" ] || touch "$backup_dir/old-was-enabled"
fi
for boot in rhm-agent.sh agy-agent.sh; do
    if [ -e "$HOME/.termux/boot/$boot" ]; then
        mv "$HOME/.termux/boot/$boot" "$backup_dir/$boot"
    fi
done
cat > "$config_dir/rollback.sh" <<EOF
#!$PREFIX/bin/bash
set -e
export SVDIR="$svdir"
touch "$new_service/down"
sv down "$new_service" || true
for boot in rhm-agent.sh agy-agent.sh; do
    if [ -f "$backup_dir/\$boot" ]; then cp "$backup_dir/\$boot" "$HOME/.termux/boot/\$boot"; fi
done
rm -f "$HOME/.termux/boot/rhm-selfhost.sh"
if [ -f "$backup_dir/old-was-enabled" ]; then
    rm -f "$old_service/down"
    sv up "$old_service"
fi
EOF
chmod 700 "$config_dir/rollback.sh"
mkdir -p "$new_service"
touch "$new_service/down"
cat > "$new_service/run" <<EOF
#!$PREFIX/bin/sh
exec "$config_dir/start.sh" >> "$config_dir/agent.log" 2>&1
EOF
chmod 700 "$new_service/run"
# Switch only after the payload, dependency, config and rollback script are ready.
trap 'echo "Connection failed; restoring previous service"; bash "$config_dir/rollback.sh"' ERR
if [ -d "$old_service" ]; then
    touch "$old_service/down"
    sv down "$old_service"
fi
export SVDIR="$svdir"
command -v termux-wake-lock >/dev/null && termux-wake-lock || true
rm -f "$new_service/down"
# Give an already running runsvdir time to discover the new directory.
for attempt in $(seq 1 10); do
    if sv up "$new_service" 2>/dev/null; then break; fi
    sleep 1
done
"$python_bin" - "$source_dir/phone.env" <<'PY'
import json,sys,time,urllib.request,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1]).parent))
from core.domain import read_env_file
c=read_env_file(sys.argv[1])
url=c['MESH_PUBLIC_URL']+'/health?user='+c['MESH_USER']
for _ in range(25):
    try:
        req=urllib.request.Request(url,headers={'Authorization':'Bearer '+c['MESH_TOKEN']})
        with urllib.request.urlopen(req,timeout=5) as r: status=json.load(r)
        if status.get('node_online'):
            print('CONNECTED: '+c['MESH_USER']); break
    except (OSError,ValueError): pass
    time.sleep(1)
else:
    raise SystemExit('Phone tunnel did not connect; check ~/.config/rhm-selfhost/agent.log')
PY
cat > "$HOME/.termux/boot/rhm-selfhost.sh" <<EOF
#!$PREFIX/bin/sh
export PATH="$PREFIX/bin:/system/bin:\$PATH"
export SVDIR="$svdir"
command -v termux-wake-lock >/dev/null && termux-wake-lock || true
command -v service-daemon >/dev/null && service-daemon start || true
sv up "$new_service"
EOF
chmod 700 "$HOME/.termux/boot/rhm-selfhost.sh"
trap - ERR
printf '\nPhone connected to the private Gateway.\nLog: %s/agent.log\nRollback: bash %s/rollback.sh\n' "$config_dir" "$config_dir"
