#!/usr/bin/env bash
# Autopilot guard: runs on an AutoDL instance and shuts it down once it has been idle long enough.
#
#   guard.sh start [idle=S] [interval=S] [grace=S] [mode=gpu|nogpu] [gpu_pct=N] [cpu_milli=N] [net_bps=N]
#   guard.sh stop | status | tick | loop
#   guard.sh job-start NAME [then_off]     start $AP/jobs/NAME.sh in the background, logging to NAME.log
#   guard.sh hold SECONDS                  count as busy for SECONDS from now (0 clears)
#   guard.sh protect NAME pid|job|cmd VALUE SECONDS   whitelist a process for SECONDS from now
#   guard.sh unprotect NAME | protected    remove a whitelist entry | list the whitelist
#   guard.sh off [--force]                 shut down now; refused while a job runs, unless --force
#   guard.sh tail NAME [LINES]
#
# Busy means: a whitelisted process is alive and its entry has not expired, the hold has not run out, or a
# reading is at or above its threshold (GPU utilisation, CPU of this container in millicores, network bytes per
# second). A reading that cannot be taken counts as busy. Any other process, a job started by autopilot run
# included, keeps the instance on only through the readings: a process that hangs without using anything does
# not keep it billing. After `idle` seconds without being busy, or `grace` seconds after the last then_off job
# ended, the instance is shut down with /usr/bin/shutdown, which on AutoDL stops the billing.
#
# The whitelist ($AP/whitelist, one entry a line: NAME TAB TYPE TAB VALUE TAB EXPIRES) protects a process by its
# PID, by the name of a job started by autopilot run, or by a piece of its command line. It lives on the data
# disk, so it holds across power-ons until it expires; a PID entry stops matching once that process is gone.
#
# Tests point AUTOPILOT_DIR, AUTOPILOT_SHUTDOWN, AUTOPILOT_NVSMI, AUTOPILOT_PROC and AUTOPILOT_CGROUP at stubs,
# and AUTOPILOT_NOW at a file holding the current unix time. Written for bash 3.2 and later.
set -u

AP=${AUTOPILOT_DIR:-/root/autodl-tmp/.autopilot}
JOBS=$AP/jobs
SHUTDOWN=${AUTOPILOT_SHUTDOWN:-/usr/bin/shutdown}
NVSMI=${AUTOPILOT_NVSMI:-nvidia-smi}
PROC=${AUTOPILOT_PROC:-/proc}
CGROUP=${AUTOPILOT_CGROUP:-/sys/fs/cgroup}
GUARD_VERSION=2

idle=900 interval=60 grace=120 mode=gpu gpu_pct=5 cpu_milli=200 net_bps=204800

now() { if [ -n "${AUTOPILOT_NOW:-}" ]; then cat "$AUTOPILOT_NOW"; else date +%s; fi; }
log() { printf '%s %s\n' "$(now)" "$*" >> "$AP/guard.log"; }
is_num() { case $1 in ''|*[!0-9]*) return 1;; esac; }

set_option() {   # set_option KEY VALUE: only known keys, only valid values
  case $1 in
    idle|interval|grace|gpu_pct|cpu_milli|net_bps) is_num "$2" || { echo "bad $1: $2" >&2; return 1; } ;;
    mode) [ "$2" = gpu ] || [ "$2" = nogpu ] || { echo "bad mode: $2" >&2; return 1; } ;;
    *) echo "unknown option: $1" >&2; return 1 ;;
  esac
  printf -v "$1" '%s' "$2"
}

read_conf() {
  [ -f "$AP/guard.conf" ] || return 0
  local k v
  while IFS='=' read -r k v; do [ -n "$k" ] && set_option "$k" "$v"; done < "$AP/guard.conf"
}

read_file() { if [ -f "$1" ]; then cat "$1"; else printf '%s' "$2"; fi; }

# ---- readings: one number each, -1 when it cannot be read ----

gpu_reading() {
  if [ "$mode" = nogpu ]; then echo 0; return; fi
  local out
  out=$("$NVSMI" --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null) || { echo -1; return; }
  printf '%s\n' "$out" | awk 'BEGIN { m = -1 } /^[ ]*[0-9]+[ ]*$/ { if ($1 + 0 > m) m = $1 + 0 } END { print m }'
}

cpu_usec() {   # CPU time this container has used, in microseconds
  if [ -f "$CGROUP/cpu.stat" ]; then
    awk '$1 == "usage_usec" { print $2; f = 1 } END { if (!f) print -1 }' "$CGROUP/cpu.stat"
  elif [ -f "$CGROUP/cpuacct/cpuacct.usage" ]; then
    awk '{ printf "%d\n", $1 / 1000 }' "$CGROUP/cpuacct/cpuacct.usage"
  else
    echo -1
  fi
}

net_bytes() {   # bytes received and sent on every interface but lo
  [ -f "$PROC/net/dev" ] || { echo -1; return; }
  sed 's/:/ /' "$PROC/net/dev" | awk 'NR > 2 && $1 != "lo" { s += $2 + $10 } END { printf "%d\n", s }'
}

# ---- jobs ----

alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }

scan_jobs() {   # sets JOBS_JSON to the JSON list of jobs and RUNNING to the names of those still running
  local first=1 f name pid rc rc_at then_off up out='['
  RUNNING=''
  for f in "$JOBS"/*.pid; do
    [ -e "$f" ] || continue
    name=$(basename "$f" .pid); pid=$(read_file "$f" '')
    rc=null; rc_at=null; then_off=false
    if [ -f "$JOBS/$name.rc" ]; then
      read -r rc rc_at < "$JOBS/$name.rc"; is_num "$rc" || rc=null; is_num "${rc_at:-}" || rc_at=null
    fi
    [ -e "$JOBS/$name.then_off" ] && then_off=true
    up=false
    if [ "$rc" = null ] && alive "$pid"; then up=true; RUNNING="$RUNNING $name"; fi
    [ $first = 1 ] || out="$out,"; first=0
    out="$out{\"name\":\"$name\",\"running\":$up,\"rc\":$rc,\"ended_at\":$rc_at,\"then_off\":$then_off}"
  done
  JOBS_JSON="$out]"
}

last_then_off_end() {   # the latest end time of a then_off job that has ended, or nothing
  local f name rc at best=''
  for f in "$JOBS"/*.then_off; do
    [ -e "$f" ] || continue
    name=$(basename "$f" .then_off)
    [ -f "$JOBS/$name.rc" ] || continue
    read -r rc at < "$JOBS/$name.rc"
    is_num "${at:-}" || continue
    if [ -z "$best" ] || [ "$at" -gt "$best" ]; then best=$at; fi
  done
  printf '%s' "$best"
}

# ---- the whitelist ----

WL=$AP/whitelist
WL_MAX_S=$(( 90 * 86400 ))

json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

job_running() {   # job_running NAME: a job started by autopilot run that has not ended
  local pid; pid=$(read_file "$JOBS/$1.pid" '')
  [ ! -f "$JOBS/$1.rc" ] && alive "$pid"
}

cmd_pids() {   # the PIDs whose command line contains the text (literally), the guard's own excluded
  local d pid line out=''
  for d in "$PROC"/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    pid=${d##*/}
    line=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    [ -n "$line" ] || continue
    case $line in *guard.sh*) continue ;; esac
    case $line in *"$1"*) out="$out $pid" ;; esac
  done
  printf '%s' "${out# }"
}

entry_pids() {   # entry_pids TYPE VALUE: the live processes an entry matches
  case $1 in
    pid) [ -d "$PROC/$2" ] && printf '%s' "$2" ;;
    job) job_running "$2" && read_file "$JOBS/$2.pid" '' ;;
    cmd) cmd_pids "$2" ;;
  esac
}

scan_whitelist() {   # sets WL_JSON (every entry and its state) and PROTECTING (names of entries in force)
  local t name type value exp pids first=1 out='[' state
  t=$(now)
  PROTECTING=''
  if [ -f "$WL" ]; then
    while IFS="$(printf '\t')" read -r name type value exp; do
      [ -n "$name" ] || continue
      pids=''
      if [ "$t" -ge "$exp" ]; then
        state=expired
      else
        pids=$(entry_pids "$type" "$value")
        if [ -n "$pids" ]; then state=protecting; PROTECTING="$PROTECTING $name"; else state=no_process; fi
      fi
      [ $first = 1 ] || out="$out,"; first=0
      out="$out{\"name\":\"$name\",\"type\":\"$type\",\"value\":\"$(json_str "$value")\",\"expires\":$exp,"
      out="$out\"expires_in\":$(( exp > t ? exp - t : 0 )),\"state\":\"$state\",\"pids\":\"$pids\"}"
    done < "$WL"
  fi
  WL_JSON="$out]"
}

cmd_protect() {
  local name=${1:-} type=${2:-} value=${3:-} secs=${4:-} tab
  tab=$(printf '\t')
  valid_name "$name" || { echo "bad entry name: $name" >&2; return 1; }
  case $type in
    pid) is_num "$value" || { echo "pid: a number" >&2; return 1; } ;;
    job) valid_name "$value" || { echo "bad job name: $value" >&2; return 1; } ;;
    cmd) [ ${#value} -ge 3 ] || { echo "cmd: at least 3 characters of the command line" >&2; return 1; }
         case $value in *"$tab"*|*"
"*) echo "cmd: no tabs or line breaks" >&2; return 1 ;; esac ;;
    *) echo "type: pid, job or cmd" >&2; return 1 ;;
  esac
  is_num "$secs" && [ "$secs" -gt 0 ] && [ "$secs" -le "$WL_MAX_S" ] || {
    echo "duration: 1 second to 90 days" >&2; return 1; }
  local exp=$(( $(now) + secs ))
  { [ -f "$WL" ] && awk -F '\t' -v n="$name" '$1 != n' "$WL"
    printf '%s\t%s\t%s\t%s\n' "$name" "$type" "$value" "$exp"; } > "$WL.new"
  mv "$WL.new" "$WL"
  log "protect: $name $type until $exp"
  scan_whitelist
  printf '{"ok":true,"whitelist":%s}\n' "$WL_JSON"
}

cmd_unprotect() {
  valid_name "${1:-}" || { echo "bad entry name: ${1:-}" >&2; return 1; }
  local found=false
  if [ -f "$WL" ] && awk -F '\t' -v n="$1" '$1 == n { f = 1 } END { exit !f }' "$WL"; then
    awk -F '\t' -v n="$1" '$1 != n' "$WL" > "$WL.new"; mv "$WL.new" "$WL"; found=true
    log "unprotect: $1"
  fi
  scan_whitelist
  printf '{"ok":true,"removed":%s,"whitelist":%s}\n' "$found" "$WL_JSON"
}

# ---- the decision ----

power_off() {   # power_off REASON
  [ -e "$AP/shutdown_sent" ] && return 0
  log "shutdown: $1"
  echo "$(now) $1" > "$AP/shutdown_sent"
  sync
  "$SHUTDOWN"
}

tick() {
  read_conf
  local t pt pcpu pnet cpu net cpu_m=-1 net_r=-1 gpu busy='' hold since idle_for end name
  t=$(now)
  read -r pt pcpu pnet < <(read_file "$AP/prev" '- - -')
  cpu=$(cpu_usec); net=$(net_bytes); gpu=$(gpu_reading)
  if is_num "$pt" && [ "$t" -gt "$pt" ]; then
    if [ "$cpu" != -1 ] && is_num "$pcpu"; then cpu_m=$(( (cpu - pcpu) / (t - pt) / 1000 )); fi
    if [ "$net" != -1 ] && is_num "$pnet"; then net_r=$(( (net - pnet) / (t - pt) )); fi
  fi
  echo "$t $cpu $net" > "$AP/prev"

  scan_jobs
  scan_whitelist
  for name in $PROTECTING; do busy="$busy \"protect:$name\""; done
  if [ "$gpu" = -1 ]; then busy="$busy \"gpu:unread\""; elif [ "$gpu" -ge "$gpu_pct" ]; then busy="$busy \"gpu:$gpu%\""; fi
  if [ "$cpu_m" = -1 ]; then busy="$busy \"cpu:unread\""; elif [ "$cpu_m" -ge "$cpu_milli" ]; then busy="$busy \"cpu:${cpu_m}m\""; fi
  if [ "$net_r" = -1 ]; then busy="$busy \"net:unread\""; elif [ "$net_r" -ge "$net_bps" ]; then busy="$busy \"net:${net_r}B/s\""; fi
  hold=$(read_file "$AP/hold_until" 0)
  if is_num "$hold" && [ "$t" -lt "$hold" ]; then busy="$busy \"hold\""; fi

  since=$(read_file "$AP/idle_since" "$t")
  is_num "$since" || since=$t
  if [ -n "$busy" ]; then since=$t; fi
  echo "$since" > "$AP/idle_since"
  idle_for=$(( t - since ))

  local verdict=running reason=''
  end=$(last_then_off_end)
  if [ -z "$RUNNING" ] && [ -n "$end" ] && [ $(( t - end )) -ge "$grace" ]; then
    verdict=shutdown; reason="then_off"
  elif [ "$idle_for" -ge "$idle" ]; then
    verdict=shutdown; reason="idle ${idle_for}s"
  fi

  printf '{"version":%s,"at":%s,"mode":"%s","idle":%s,"idle_for":%s,"shutdown_in":%s,"busy":[%s],"gpu_pct":%s,"cpu_milli":%s,"net_bps":%s,"hold_until":%s,"jobs":%s,"whitelist":%s,"verdict":"%s"}\n' \
    "$GUARD_VERSION" "$t" "$mode" "$idle" "$idle_for" "$(( idle - idle_for > 0 ? idle - idle_for : 0 ))" \
    "$(echo $busy | sed 's/" "/","/g')" "$gpu" "$cpu_m" "$net_r" "${hold:-0}" "$JOBS_JSON" "$WL_JSON" "$verdict" > "$AP/status.json.tmp"
  mv "$AP/status.json.tmp" "$AP/status.json"
  [ "$verdict" = shutdown ] && power_off "$reason"
  return 0
}

# ---- commands ----

daemon_pid() { read_file "$AP/guard.pid" ''; }

daemon_alive() {
  local pid; pid=$(daemon_pid)
  alive "$pid" || return 1
  [ -r "$PROC/$pid/cmdline" ] || return 0
  tr '\0' ' ' < "$PROC/$pid/cmdline" | grep -q 'guard.sh loop'
}

cmd_stop() {
  if daemon_alive; then kill "$(daemon_pid)" 2>/dev/null; fi
  rm -f "$AP/guard.pid"
}

cmd_start() {
  local kv
  : > "$AP/guard.conf.new"
  for kv in "$@"; do
    set_option "${kv%%=*}" "${kv#*=}" || { rm -f "$AP/guard.conf.new"; return 1; }
    printf '%s\n' "$kv" >> "$AP/guard.conf.new"
  done
  mv "$AP/guard.conf.new" "$AP/guard.conf"
  cmd_stop
  rm -f "$AP/prev" "$AP/shutdown_sent"
  now > "$AP/idle_since"
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup bash "$AP/guard.sh" loop > /dev/null 2>&1 < /dev/null &
  else
    nohup bash "$AP/guard.sh" loop > /dev/null 2>&1 < /dev/null &
  fi
  echo $! > "$AP/guard.pid"
  log "started: $*"
  cmd_status
}

cmd_loop() {
  echo $$ > "$AP/guard.pid"
  while :; do
    [ "$(daemon_pid)" = "$$" ] || exit 0      # a newer start replaced this loop
    tick
    [ -e "$AP/shutdown_sent" ] && exit 0
    read_conf
    sleep "$interval"
  done
}

cmd_status() {
  local up=false
  daemon_alive && up=true
  scan_jobs
  scan_whitelist
  printf '{"now":%s,"guard_version":%s,"daemon_running":%s,"last":%s,"jobs":%s,"whitelist":%s}\n' \
    "$(now)" "$GUARD_VERSION" "$up" "$(read_file "$AP/status.json" null)" "$JOBS_JSON" "$WL_JSON"
}

valid_name() { case $1 in ''|.*|*[!A-Za-z0-9._-]*) return 1;; esac; [ ${#1} -le 64 ]; }

cmd_job_start() {
  local name=${1:-} then_off=${2:-}
  valid_name "$name" || { echo "bad job name: $name" >&2; return 1; }
  [ -f "$JOBS/$name.sh" ] || { echo "no job script $JOBS/$name.sh" >&2; return 1; }
  local pid; pid=$(read_file "$JOBS/$name.pid" '')
  if [ ! -f "$JOBS/$name.rc" ] && alive "$pid"; then
    printf '{"ok":false,"refused":"job %s is still running"}\n' "$name"; return 3
  fi
  rm -f "$JOBS/$name.rc" "$JOBS/$name.log" "$JOBS/$name.then_off"
  [ "$then_off" = then_off ] && touch "$JOBS/$name.then_off"
  local run='bash "$1" > "$2" 2>&1; echo "$? $(date +%s)" > "$3"'
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup bash -c "$run" job "$JOBS/$name.sh" "$JOBS/$name.log" "$JOBS/$name.rc" > /dev/null 2>&1 < /dev/null &
  else
    nohup bash -c "$run" job "$JOBS/$name.sh" "$JOBS/$name.log" "$JOBS/$name.rc" > /dev/null 2>&1 < /dev/null &
  fi
  echo $! > "$JOBS/$name.pid"
  now > "$AP/idle_since"
  log "job started: $name ($then_off)"
  printf '{"ok":true,"job":"%s","pid":%s}\n' "$name" "$(cat "$JOBS/$name.pid")"
}

cmd_hold() {
  is_num "${1:-}" || { echo "hold: seconds" >&2; return 1; }
  if [ "$1" = 0 ]; then rm -f "$AP/hold_until"; else echo $(( $(now) + $1 )) > "$AP/hold_until"; fi
  log "hold: $1"
  printf '{"ok":true,"hold_until":%s}\n' "$(read_file "$AP/hold_until" 0)"
}

cmd_off() {
  scan_jobs
  if [ -n "$RUNNING" ] && [ "${1:-}" != --force ]; then
    printf '{"ok":false,"refused":"jobs are running","running":"%s"}\n' "${RUNNING# }"; return 3
  fi
  log "off requested${RUNNING:+, cutting:$RUNNING}"
  echo "$(now) off" > "$AP/shutdown_sent"
  ( sleep 2; sync; "$SHUTDOWN" ) > /dev/null 2>&1 < /dev/null &
  printf '{"ok":true}\n'
}

cmd_tail() {
  valid_name "${1:-}" || { echo "bad job name: ${1:-}" >&2; return 1; }
  local f=$JOBS/$1.log
  [ "$1" = guard ] && f=$AP/guard.log
  [ -f "$f" ] || { echo "no log: $f" >&2; return 1; }
  tail -n "${2:-50}" "$f"
}

mkdir -p "$AP" "$JOBS"
cmd=${1:-status}; [ $# -gt 0 ] && shift
case $cmd in
  start) cmd_start "$@" ;;
  stop) cmd_stop ;;
  loop) read_conf; cmd_loop ;;
  tick) tick ;;
  status) read_conf; cmd_status ;;
  job-start) cmd_job_start "$@" ;;
  hold) cmd_hold "$@" ;;
  protect) cmd_protect "$@" ;;
  unprotect) cmd_unprotect "$@" ;;
  protected) scan_whitelist; printf '{"whitelist":%s}\n' "$WL_JSON" ;;
  off) cmd_off "$@" ;;
  tail) cmd_tail "$@" ;;
  *) echo "usage: guard.sh start|stop|status|tick|loop|job-start|hold|protect|unprotect|protected|off|tail" >&2; exit 1 ;;
esac
