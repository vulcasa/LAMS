#!/usr/bin/env bash
# LAMS one-click installer
# Supported: Debian/Ubuntu, RHEL/Fedora/Rocky/Alma, Arch, openSUSE, macOS (brew/launchd)
set -Eeuo pipefail
IFS=$'\n\t'

LAMS_REPO="${LAMS_REPO:-https://github.com/vulcasa/LAMS.git}"
LAMS_REF="${LAMS_REF:-main}"
LAMS_HOME="${LAMS_HOME:-/opt/lams}"
LAMS_DATA="${LAMS_DATA:-/lams/data}"
LAMS_PORT="${LAMS_PORT:-5080}"
LAMS_TAPE_DEVICE="${LAMS_TAPE_DEVICE:-/dev/sg10}"
LAMS_THREADS="${LAMS_THREADS:-12}"
LAMS_SKIP_LTFS="${LAMS_SKIP_LTFS:-0}"
LAMS_UPDATE=0

say()  { printf '\033[1;34m[LAMS]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "安装失败（行 ${LINENO}），请检查上方输出"' ERR

usage() {
  cat <<EOF
LAMS 一键安装脚本

用法:
  sudo bash install.sh [选项]

选项:
  --repo URL          GitHub 项目地址（默认: ${LAMS_REPO}）
  --ref REF           分支/标签（默认: ${LAMS_REF}）
  --home DIR          程序目录（默认: ${LAMS_HOME}）
  --data DIR          数据目录（默认: ${LAMS_DATA}）
  --port PORT         Web 端口（默认: ${LAMS_PORT}）
  --tape-device DEV   磁带设备（默认: ${LAMS_TAPE_DEVICE}）
  --threads N         Gunicorn 线程数（默认: ${LAMS_THREADS}）
  --skip-ltfs         不编译 LTFS（仅媒体库，不启用磁带）
  --update            更新已有安装（会尝试 fast-forward）
  -h, --help          显示帮助

安装完成后:
  http://<本机IP>:${LAMS_PORT}
  数据目录: ${LAMS_DATA}
  服务名: lams
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) LAMS_REPO="$2"; shift 2 ;;
    --ref) LAMS_REF="$2"; shift 2 ;;
    --home) LAMS_HOME="$2"; shift 2 ;;
    --data) LAMS_DATA="$2"; shift 2 ;;
    --port) LAMS_PORT="$2"; shift 2 ;;
    --tape-device) LAMS_TAPE_DEVICE="$2"; shift 2 ;;
    --threads) LAMS_THREADS="$2"; shift 2 ;;
    --skip-ltfs) LAMS_SKIP_LTFS=1; shift ;;
    --update) LAMS_UPDATE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

# macOS: Homebrew 拒绝 root，依赖安装需要普通用户；Linux 直接要求 root。
if [[ "$(uname -s)" == "Linux" && "${EUID:-$(id -u)}" -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi
if [[ "$(uname -s)" == "Darwin" && "${EUID:-$(id -u)}" -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

OS="$(uname -s)"
DISTRO_ID=""
DISTRO_LIKE=""
if [[ "$OS" == "Linux" && -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO_ID="${ID:-}"
  DISTRO_LIKE="${ID_LIKE:-}"
fi

have() { command -v "$1" >/dev/null 2>&1; }

pick_python() {
  local candidate
  for candidate in "${LAMS_PYTHON:-}" /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    [[ -n "$candidate" ]] || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

install_linux_packages() {
  local pm=""
  if have apt-get; then pm=apt
  elif have dnf; then pm=dnf
  elif have yum; then pm=yum
  elif have pacman; then pm=pacman
  elif have zypper; then pm=zypper
  else die "未识别的包管理器（支持 apt/dnf/yum/pacman/zypper）"
  fi
  say "使用包管理器: $pm"

  case "$pm" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -y
      apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip git curl ca-certificates \
        ffmpeg cifs-utils sg3-utils mt-st util-linux \
        build-essential autoconf automake libtool pkg-config \
        libfuse-dev libxml2-dev libssl-dev libsnmp-dev libicu-dev uuid-dev \
        openssl
      ;;
    dnf|yum)
      $pm install -y epel-release || true
      $pm install -y \
        python3 python3-pip python3-devel git curl ca-certificates \
        cifs-utils sg3_utils mt-st util-linux \
        gcc gcc-c++ make autoconf automake libtool pkgconf-pkg-config \
        fuse-devel libxml2-devel openssl-devel net-snmp-devel libicu-devel libuuid-devel \
        openssl
      # ffmpeg 在 RHEL 系可能来自 EPEL/ffmpeg-free；可选
      $pm install -y ffmpeg || $pm install -y ffmpeg-free || warn "ffmpeg 未安装成功，抽帧/缩略图功能会不可用"
      ;;
    pacman)
      pacman -Sy --noconfirm --needed \
        python python-pip git curl ca-certificates ffmpeg cifs-utils sg3_utils mt-st util-linux \
        base-devel autoconf automake libtool pkgconf fuse libxml2 openssl net-snmp icu
      ;;
    zypper)
      zypper --non-interactive refresh || true
      zypper --non-interactive install -y \
        python3 python3-pip python3-devel git curl ca-certificates ffmpeg cifs-utils sg3_utils mt-st util-linux \
        gcc gcc-c++ make autoconf automake libtool pkg-config fuse-devel libxml2-devel libopenssl-devel net-snmp-devel libicu-devel uuid-devel openssl
      ;;
  esac
}

install_macos_packages() {
  have brew || die "未找到 Homebrew，请先安装: https://brew.sh/"
  local brew_cmd=(brew)
  if [[ "${EUID:-$(id -u)}" -eq 0 && -n "${SUDO_USER:-}" ]]; then
    brew_cmd=(sudo -u "$SUDO_USER" -H brew)
  fi
  say "使用 Homebrew 安装依赖"
  "${brew_cmd[@]}" install python@3.12 git curl ffmpeg || true
  # Homebrew Python 可能不在 root PATH 中；记录一个可用 python
  if have python3; then :; elif [[ -x /opt/homebrew/bin/python3 ]]; then
    export PATH="/opt/homebrew/bin:$PATH"
  fi
}

install_packages() {
  if [[ "$OS" == "Darwin" ]]; then
    install_macos_packages
  else
    install_linux_packages
  fi
}

install_project() {
  say "安装项目到 $LAMS_HOME"
  mkdir -p "$(dirname "$LAMS_HOME")"
  if [[ -d "$LAMS_HOME/.git" ]]; then
    if [[ "$LAMS_UPDATE" -ne 1 ]]; then
      die "$LAMS_HOME 已是 Git 仓库；如需更新请加 --update"
    fi
    if ! git -C "$LAMS_HOME" diff --quiet || ! git -C "$LAMS_HOME" diff --cached --quiet; then
      die "$LAMS_HOME 有未提交修改，拒绝自动更新；请先备份/提交"
    fi
    say "拉取最新代码"
    git -C "$LAMS_HOME" fetch --depth 1 origin "$LAMS_REF"
    if git -C "$LAMS_HOME" rev-parse --verify "origin/$LAMS_REF" >/dev/null 2>&1; then
      git -C "$LAMS_HOME" checkout "$LAMS_REF"
      git -C "$LAMS_HOME" merge --ff-only "origin/$LAMS_REF"
    else
      git -C "$LAMS_HOME" checkout --detach "$LAMS_REF"
    fi
  elif [[ -e "$LAMS_HOME" && -n "$(ls -A "$LAMS_HOME" 2>/dev/null || true)" ]]; then
    die "$LAMS_HOME 已存在且不是 Git 仓库，拒绝覆盖"
  else
    say "从 GitHub 克隆: $LAMS_REPO ($LAMS_REF)"
    git clone --depth 1 --branch "$LAMS_REF" "$LAMS_REPO" "$LAMS_HOME"
  fi
  mkdir -p "$LAMS_HOME/logs" "$LAMS_DATA"
  chmod 755 "$LAMS_DATA"
}

install_python_deps() {
  PYTHON_BIN="$(pick_python || true)"
  [[ -n "${PYTHON_BIN:-}" ]] || die "未找到 Python 3.10+；请先安装 Python 3.10+"
  local ver
  ver="$("$PYTHON_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  say "Python: $ver ($PYTHON_BIN)"
  if [[ ! -x "$LAMS_HOME/.venv/bin/python" ]]; then
    say "创建 Python venv"
    "$PYTHON_BIN" -m venv "$LAMS_HOME/.venv"
  fi
  "$LAMS_HOME/.venv/bin/pip" install --upgrade pip wheel
  "$LAMS_HOME/.venv/bin/pip" install -r "$LAMS_HOME/requirements-prod.txt"
}

build_ltfs() {
  LTFS_STATUS="not-installed"
  if [[ "$OS" == "Darwin" ]]; then
    LTFS_STATUS="unsupported-macos"
    warn "macOS 不支持 LTFS，磁带功能不可用；媒体库功能仍可使用"
    return 0
  fi
  if [[ "$LAMS_SKIP_LTFS" -eq 1 ]]; then
    LTFS_STATUS="skipped"
    warn "按要求跳过 LTFS 安装"
    return 0
  fi
  if have ltfs && have mkltfs; then
    LTFS_STATUS="existing"
    say "检测到已有 LTFS，跳过编译"
    return 0
  fi
  local ltfs_tar uthash_tar build
  ltfs_tar="$(find "$LAMS_HOME/vendor" -maxdepth 1 -name 'ltfs-*.tar.gz' | sort | head -1 || true)"
  uthash_tar="$(find "$LAMS_HOME/vendor" -maxdepth 1 -name 'uthash-*.tar.gz' | sort | head -1 || true)"
  [[ -n "$ltfs_tar" && -n uthash_tar ]] || { LTFS_STATUS="missing-source"; warn "vendor 中缺少 LTFS 源码，磁带功能不可用"; return 0; }
  build="$(mktemp -d /tmp/lams-ltfs.XXXXXX)"
  say "编译 LTFS（首次可能需要几分钟）"
  mkdir -p "$build/ltfs"
  tar xzf "$ltfs_tar" -C "$build/ltfs" --strip-components=1
  mkdir -p "$build/ltfs/src/libltfs/uthash_submodule"
  tar xzf "$uthash_tar" -C "$build/ltfs/src/libltfs/uthash_submodule" --strip-components=1
  (
    cd "$build/ltfs"
    ./autogen.sh
    ./configure --prefix=/usr --sysconfdir=/etc
    make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
    make install
  )
  have ldconfig && ldconfig || true
  rm -rf "$build"
  if have ltfs && have mkltfs; then
    LTFS_STATUS="built"
  else
    LTFS_STATUS="build-failed"
    warn "LTFS 编译完成但命令不可用；磁带功能可能不可用"
  fi
}

write_env() {
  say "写入运行配置 /etc/lams/lams.env"
  install -d -m 700 /etc/lams
  local secret
  if have openssl; then
    secret="$(openssl rand -hex 32)"
  else
    secret="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  fi
  {
    echo "AVLIB_DATA_DIR=$LAMS_DATA"
    echo "AVLIB_DB_PATH=$LAMS_DATA/avlib.db"
    echo "AVLIB_MANIFEST_DIR=$LAMS_DATA/manifests"
    echo "AVLIB_MEDIA_DIR=$LAMS_DATA/media"
    echo "AVLIB_DUMP_DIR=$LAMS_DATA/dump"
    echo "AVLIB_R18_DB=$LAMS_DATA/r18dump.db"
    echo "AVLIB_TAPE_ADAPTER=direct"
    echo "LTFS_DEVICE=$LAMS_TAPE_DEVICE"
    echo "LTFS_MOUNT=/media/tape"
    echo "AVLIB_AUTO_EJECT=0"
    echo "AVLIB_WORKER=1"
    echo "AVLIB_BROWSE_ROOTS=/mnt,$LAMS_DATA"
    echo "AVLIB_SECRET_KEY=$secret"
    echo "TZ=Asia/Shanghai"
  } > /etc/lams/lams.env
  chmod 600 /etc/lams/lams.env
}

open_firewall() {
  if [[ "$OS" == "Darwin" ]]; then
    warn "macOS 防火墙未自动修改；如开启应用防火墙，请允许 Python/gunicorn 监听 $LAMS_PORT"
    return 0
  fi
  say "尝试开放 TCP $LAMS_PORT"
  if have ufw; then
    ufw allow "$LAMS_PORT/tcp" || warn "ufw 开放端口失败"
  elif have firewall-cmd; then
    firewall-cmd --permanent --add-port="$LAMS_PORT/tcp" || true
    firewall-cmd --reload || true
  elif have nft; then
    nft add rule inet filter input tcp dport "$LAMS_PORT" accept || warn "nftables 规则未持久化，请手动保存"
  elif have iptables; then
    iptables -C INPUT -p tcp --dport "$LAMS_PORT" -j ACCEPT 2>/dev/null || \
      iptables -I INPUT -p tcp --dport "$LAMS_PORT" -j ACCEPT || warn "iptables 开放端口失败"
    warn "iptables 规则可能不持久，请根据发行版保存规则"
  else
    warn "未检测到防火墙工具；请确认云厂商/路由器安全组允许 TCP $LAMS_PORT"
  fi
}

install_service_linux() {
  have systemctl || die "未检测到 systemd，无法注册开机自启服务；可手动运行 gunicorn"
  say "写入 systemd 服务"
  cat > /etc/systemd/system/lams.service <<EOF
[Unit]
Description=LAMS - Local AV Management System
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$LAMS_HOME
EnvironmentFile=-/etc/lams/lams.env
ExecStart=$LAMS_HOME/.venv/bin/gunicorn -b 0.0.0.0:$LAMS_PORT -w 1 --threads $LAMS_THREADS --timeout 300 wsgi:app
Restart=always
RestartSec=3
LimitNOFILE=65535
StandardOutput=append:$LAMS_HOME/logs/lams.log
StandardError=append:$LAMS_HOME/logs/lams.log

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable lams.service
  systemctl restart lams.service
}

install_service_macos() {
  say "写入 launchd 服务"
  local secret
  secret="$(sed -n 's/^AVLIB_SECRET_KEY=//p' /etc/lams/lams.env | head -1)"
  cat > /Library/LaunchDaemons/com.lams.archive.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.lams.archive</string>
  <key>WorkingDirectory</key><string>$LAMS_HOME</string>
  <key>ProgramArguments</key>
  <array>
    <string>$LAMS_HOME/.venv/bin/gunicorn</string>
    <string>-b</string><string>0.0.0.0:$LAMS_PORT</string>
    <string>-w</string><string>1</string>
    <string>--threads</string><string>$LAMS_THREADS</string>
    <string>--timeout</string><string>300</string>
    <string>wsgi:app</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AVLIB_DATA_DIR</key><string>$LAMS_DATA</string>
    <key>AVLIB_DB_PATH</key><string>$LAMS_DATA/avlib.db</string>
    <key>AVLIB_MANIFEST_DIR</key><string>$LAMS_DATA/manifests</string>
    <key>AVLIB_MEDIA_DIR</key><string>$LAMS_DATA/media</string>
    <key>AVLIB_DUMP_DIR</key><string>$LAMS_DATA/dump</string>
    <key>AVLIB_R18_DB</key><string>$LAMS_DATA/r18dump.db</string>
    <key>AVLIB_TAPE_ADAPTER</key><string>direct</string>
    <key>AVLIB_AUTO_EJECT</key><string>0</string>
    <key>AVLIB_WORKER</key><string>1</string>
    <key>AVLIB_BROWSE_ROOTS</key><string>/mnt,$LAMS_DATA</string>
    <key>AVLIB_SECRET_KEY</key><string>$secret</string>
    <key>TZ</key><string>Asia/Shanghai</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LAMS_HOME/logs/lams.log</string>
  <key>StandardErrorPath</key><string>$LAMS_HOME/logs/lams.log</string>
</dict>
</plist>
EOF
  launchctl bootout system/com.lams.archive 2>/dev/null || true
  launchctl bootstrap system /Library/LaunchDaemons/com.lams.archive.plist
  launchctl enable system/com.lams.archive
  launchctl kickstart -k system/com.lams.archive
}

service_status() {
  if [[ "$OS" == "Darwin" ]]; then
    launchctl print system/com.lams.archive 2>/dev/null | head -5 || true
  else
    systemctl --no-pager --full status lams.service | head -12 || true
  fi
}

health_check() {
  say "等待服务启动..."
  local i
  for i in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:$LAMS_PORT/healthz" >/dev/null 2>&1; then
      say "服务健康检查通过"
      return 0
    fi
    sleep 1
  done
  service_status
  die "服务未在 40 秒内通过 /healthz；请查看 $LAMS_HOME/logs/lams.log"
}

print_summary() {
  local ip
  if [[ "$OS" == "Darwin" ]]; then
    ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"
  else
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1)"
  fi
  cat <<EOF

============================================================
LAMS 安装完成
------------------------------------------------------------
程序目录: $LAMS_HOME
数据目录: $LAMS_DATA
监听:     0.0.0.0:$LAMS_PORT
访问:     http://$ip:$LAMS_PORT
LTFS:     $LTFS_STATUS
服务:     $([[ "$OS" == "Darwin" ]] && echo com.lams.archive || echo lams.service)
日志:     $LAMS_HOME/logs/lams.log

防火墙/安全组请确认允许 TCP ${LAMS_PORT}。
首次使用请先配置 NAS/SMB 媒体库，再执行扫描。
注意：LAMS 默认无登录认证，建议仅在内网或带认证的反向代理后使用。
============================================================
EOF
}

say "LAMS installer 开始"
say "系统: $OS ${DISTRO_ID:+($DISTRO_ID)}"
install_packages
install_project
install_python_deps
build_ltfs
write_env
open_firewall
if [[ "$OS" == "Darwin" ]]; then
  install_service_macos
else
  install_service_linux
fi
health_check
print_summary
