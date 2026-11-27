#!/usr/bin/env bash
# selfcheck.sh — 渲染后自检（CLAUDE.md：每次渲染完自检）。
#
#   1. ffprobe 核对时长、分辨率、帧率（和期望值比；期望值来自 --project 或参数）
#   2. 整片扫描：黑帧、空底色帧（整帧几乎同一亮度）、长时间静止（动画提前结束？）
#   3. 按时间点抽帧到 frames/，并拼一张总览图 contact-sheet.png
#   4. 报告同时写进 frames/selfcheck.txt
#
# 脚本只负责量；文字溢出、重叠、乱码这类问题仍要人（或 agent）逐张看抽帧。
# 退出码：0 通过（可能有警告），1 不通过，2 用法错误。
# 依赖：ffmpeg / ffprobe、perl、awk（macOS 自带的 bash 3.2 即可）。
set -euo pipefail

usage() {
  cat <<'EOF'
用法: scripts/selfcheck.sh VIDEO [选项]

期望值（不给就只报告、不核对）:
  --project DIR|FILE   HyperFrames 工程目录或 index.html：读根节点的
                       data-duration / data-width / data-height / data-fps（缺省 30）
  --duration SEC       期望时长（覆盖 --project）
  --size WxH           期望分辨率，如 1920x1080
  --fps N              期望帧率，如 30 或 30000/1001
  --tolerance SEC      时长容差，默认 0.3
  --audio yes|no       期望有 / 没有音轨

抽帧:
  --at T1,T2,...       抽帧时间点（秒）；默认在 0.5 s 到片尾前 0.5 s 之间均匀取 --count 张
  --count N            默认 7
  --frames-dir DIR     默认 <视频所在目录>/frames（会先删掉其中旧的 t*s.png）

整片扫描:
  --no-scan            跳过整片扫描（长素材时省时间）
  --freeze SEC         报告静止超过 SEC 秒的段落，默认 2
  --flat-min SEC       空底色帧连续超过 SEC 秒才警告，默认 0.2（更短的算转场）
  --strict             有警告也算不通过

例:
  scripts/selfcheck.sh videos/001-demo/renders/demo.mp4 --project videos/001-demo/src \
      --at 0.5,2,4.5,7,9.5,12,14.5 --audio no
EOF
}

die_usage() {
  echo "selfcheck: $*" >&2
  echo "运行 scripts/selfcheck.sh --help 查看用法" >&2
  exit 2
}

is_number() { case "$1" in '' | *[!0-9.]* | *.*.*) return 1 ;; *) return 0 ;; esac; }
# |a-b| <= t
within() { awk -v a="$1" -v b="$2" -v t="$3" 'BEGIN { d = a - b; if (d < 0) d = -d; exit !(d <= t) }'; }
# "30000/1001" or "30" -> decimal
rate() { awk -v r="$1" 'BEGIN { n = split(r, p, "/"); if (n == 2 && p[2] > 0) printf "%.6g", p[1] / p[2]; else printf "%.6g", r + 0 }'; }

video="" project="" exp_dur="" exp_size="" exp_fps="" exp_audio=""
tol=0.3 at="" count=7 frames_dir="" scan=1 freeze_min=2 flat_min=0.2 strict=0

need_value() { [ $# -ge 2 ] && [ -n "$2" ] || die_usage "$1 需要一个值"; }
while [ $# -gt 0 ]; do
  case "$1" in
    -h | --help) usage; exit 0 ;;
    --project) need_value "$@"; project="$2"; shift 2 ;;
    --duration) need_value "$@"; exp_dur="$2"; shift 2 ;;
    --size) need_value "$@"; exp_size="$2"; shift 2 ;;
    --fps) need_value "$@"; exp_fps="$2"; shift 2 ;;
    --tolerance) need_value "$@"; tol="$2"; shift 2 ;;
    --audio) need_value "$@"; exp_audio="$2"; shift 2 ;;
    --at) need_value "$@"; at="$2"; shift 2 ;;
    --count) need_value "$@"; count="$2"; shift 2 ;;
    --frames-dir) need_value "$@"; frames_dir="$2"; shift 2 ;;
    --no-scan) scan=0; shift ;;
    --freeze) need_value "$@"; freeze_min="$2"; shift 2 ;;
    --flat-min) need_value "$@"; flat_min="$2"; shift 2 ;;
    --strict) strict=1; shift ;;
    -*) die_usage "未知选项 $1" ;;
    *)
      [ -z "$video" ] || die_usage "多余的参数 $1"
      video="$1"; shift ;;
  esac
done

[ -n "$video" ] || { usage >&2; exit 2; }
[ -f "$video" ] || die_usage "找不到视频文件 $video"
for v in "$tol" "$freeze_min" "$flat_min" "$count"; do is_number "$v" || die_usage "不是数字: $v"; done
[ -z "$exp_dur" ] || is_number "$exp_dur" || die_usage "--duration 不是数字: $exp_dur"
case "$exp_size" in '' | [0-9]*x[0-9]*) ;; *) die_usage "--size 要写成 WxH，如 1920x1080" ;; esac
case "$exp_audio" in '' | yes | no) ;; *) die_usage "--audio 只能是 yes 或 no" ;; esac
for tool in ffmpeg ffprobe perl awk; do
  command -v "$tool" >/dev/null 2>&1 || die_usage "缺少 $tool"
done

# ---------- expectations from the HyperFrames project ----------
if [ -n "$project" ]; then
  index="$project"
  [ -d "$index" ] && index="$index/index.html"
  [ -f "$index" ] || die_usage "找不到 $index"
  p_dur="" p_w="" p_h="" p_fps=""
  # The first tag carrying data-composition-id is the root composition.
  while IFS='=' read -r key value; do
    case "$key" in
      duration) p_dur="$value" ;;
      width) p_w="$value" ;;
      height) p_h="$value" ;;
      fps) p_fps="$value" ;;
    esac
  done < <(perl -0777 -ne '
    if (/<[a-z][^>]*\bdata-composition-id="[^"]*"[^>]*>/is) {
      my $tag = $&;
      for my $a (qw(duration width height fps)) {
        print "$a=$1\n" if $tag =~ /\bdata-$a="([0-9]+(?:\.[0-9]+)?(?:\/[0-9]+)?)"/i;
      }
    }' "$index")
  [ -n "$exp_dur" ] || exp_dur="$p_dur"
  [ -n "$exp_size" ] || { [ -n "$p_w" ] && [ -n "$p_h" ] && exp_size="${p_w}x${p_h}"; } || true
  [ -n "$exp_fps" ] || exp_fps="${p_fps:-30}"
fi

# ---------- output locations ----------
[ -n "$frames_dir" ] || frames_dir="$(cd "$(dirname "$video")" && pwd)/frames"
mkdir -p "$frames_dir"
frames_dir="$(cd "$frames_dir" && pwd)"
report="$frames_dir/selfcheck.txt"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/selfcheck.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

shopt -s nullglob
old=("$frames_dir"/t[0-9]*s.png "$frames_dir/contact-sheet.png")
[ ${#old[@]} -eq 0 ] || rm -f "${old[@]}"
shopt -u nullglob
: >"$report"

fails=0 warns=0
say() { printf '%s\n' "$*"; printf '%s\n' "$*" >>"$report"; }
ok() { say "  ✓ $*"; }
info() { say "  · $*"; }
warn() { say "  ⚠ $*"; warns=$((warns + 1)); }
fail() { say "  ✗ $*"; fails=$((fails + 1)); }

say "selfcheck  $video"
say "时间      $(date '+%Y-%m-%d %H:%M:%S')"
[ -z "$project" ] || say "工程      $index"

# ---------- 1. ffprobe ----------
say ""
say "[1] ffprobe"
codec="" profile="" pix_fmt="" width="" height="" r_rate="" avg_rate="" nb_frames="" s_dur=""
while IFS='=' read -r key value; do
  case "$key" in
    codec_name) codec="$value" ;;
    profile) profile="$value" ;;
    pix_fmt) pix_fmt="$value" ;;
    width) width="$value" ;;
    height) height="$value" ;;
    r_frame_rate) r_rate="$value" ;;
    avg_frame_rate) avg_rate="$value" ;;
    nb_frames) nb_frames="$value" ;;
    duration) s_dur="$value" ;;
  esac
done < <(ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration \
  -of default=noprint_wrappers=1 "$video" 2>/dev/null || true)

if [ -z "$width" ]; then
  fail "读不到视频流（文件损坏或没有视频轨）"
  say ""
  say "结果：不通过（$fails 项失败）"
  exit 1
fi

f_dur="$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$video" 2>/dev/null || true)"
dur="$s_dur"
is_number "$dur" || dur="$f_dur"
is_number "$dur" || dur=0
fps="$(rate "$r_rate")"
audio_streams="$(ffprobe -v error -select_streams a -show_entries stream=codec_name -of csv=p=0 "$video" 2>/dev/null | awk 'NF' | wc -l | tr -d ' ')"
size_bytes="$(wc -c <"$video" | tr -d ' ')"

info "$codec ${profile:-} · $pix_fmt · ${width}x${height} · ${r_rate} fps · ${dur} s · ${nb_frames:-?} 帧 · $(awk -v b="$size_bytes" 'BEGIN { printf "%.2f MB", b / 1048576 }') · 音轨 $audio_streams 条"

if [ -n "$exp_dur" ]; then
  if within "$dur" "$exp_dur" "$tol"; then ok "时长 ${dur} s（期望 ${exp_dur} ± ${tol} s）"
  else fail "时长 ${dur} s，期望 ${exp_dur} ± ${tol} s"; fi
else
  info "没给期望时长，未核对"
fi

if [ -n "$exp_size" ]; then
  if [ "${width}x${height}" = "$exp_size" ]; then ok "分辨率 ${width}x${height}"
  else fail "分辨率 ${width}x${height}，期望 $exp_size"; fi
else
  info "没给期望分辨率，未核对"
fi

if [ -n "$exp_fps" ]; then
  want_fps="$(rate "$exp_fps")"
  if within "$fps" "$want_fps" 0.01; then ok "帧率 ${r_rate}（期望 ${exp_fps}）"
  else fail "帧率 ${r_rate}，期望 $exp_fps"; fi
else
  info "没给期望帧率，未核对"
fi
if [ -n "$avg_rate" ] && [ "$avg_rate" != "0/0" ] && ! within "$(rate "$avg_rate")" "$fps" 0.01; then
  warn "平均帧率 $avg_rate 和标称 $r_rate 不一致（可变帧率？）"
fi
if is_number "${nb_frames:-x}"; then
  want_frames="$(awk -v d="$dur" -v f="$fps" 'BEGIN { printf "%d", d * f + 0.5 }')"
  if ! within "$nb_frames" "$want_frames" 1; then
    warn "帧数 ${nb_frames}，按时长 × 帧率应为 ${want_frames}（掉帧或重复帧？）"
  fi
fi
case "$pix_fmt" in
  yuv420p | yuvj420p) ;;
  *) warn "像素格式 ${pix_fmt}：很多播放器 / 平台只认 yuv420p" ;;
esac
case "$exp_audio" in
  yes) [ "$audio_streams" -gt 0 ] && ok "有音轨" || fail "期望有音轨，实际没有" ;;
  no) [ "$audio_streams" -eq 0 ] && ok "无音轨（静音片）" || fail "期望静音片，实际有 $audio_streams 条音轨" ;;
esac

# ---------- 2. whole-video scan ----------
say ""
say "[2] 整片扫描"
if [ "$scan" -eq 0 ]; then
  info "已跳过（--no-scan）"
else
  stats="$tmp/stats.txt"
  scan_log="$tmp/scan.log"
  # freezedetect noise floor: at -60dB a thin moving element (a playhead, a line fading in)
  # still reads as "frozen"; -80dB was measured on 001-demo to report only real holds.
  if ! ffmpeg -hide_banner -nostats -v info -i "$video" -an \
    -vf "signalstats,metadata=mode=print:file=$stats,freezedetect=n=-80dB:d=$freeze_min" \
    -f null - 2>"$scan_log"; then
    warn "整片扫描失败：$(tail -1 "$scan_log")"
  else
    # Per frame: black = nothing brighter than near-black; flat = the whole frame within a
    # narrow luma band (a bare background of any colour, i.e. nothing rendered on it).
    awk -v fps="$fps" -v black_y=24 -v flat_th=12 '
      function emit() {
        cls = (ymax < black_y) ? "black" : ((ymax - ymin) < flat_th ? "flat" : "ok")
        frames++
        if (cls != cur) {
          if (cur != "" && cur != "ok") printf "%s %.3f %.3f\n", cur, start, t
          cur = cls; start = t
        }
        last = t
      }
      /^frame:/ {
        if (have) emit()
        have = 1
        n = split($0, f, /[ \t]+/)
        for (i = 1; i <= n; i++) if (f[i] ~ /^pts_time:/) t = substr(f[i], 10) + 0
      }
      /signalstats\.YMIN=/ { ymin = substr($0, index($0, "=") + 1) + 0 }
      /signalstats\.YMAX=/ { ymax = substr($0, index($0, "=") + 1) + 0 }
      END {
        if (have) emit()
        if (cur != "" && cur != "ok") printf "%s %.3f %.3f\n", cur, start, last + 1 / fps
        printf "frames %d\n", frames
      }' "$stats" >"$tmp/runs.txt"

    scanned="$(awk '$1 == "frames" { print $2 }' "$tmp/runs.txt")"
    short_flat=0
    black_runs=0
    while read -r kind a b; do
      case "$kind" in
        black)
          black_runs=$((black_runs + 1))
          warn "黑帧 ${a}–${b} s（$(awk -v a="$a" -v b="$b" 'BEGIN { printf "%.2f", b - a }') s）：故意的淡入淡出？" ;;
        flat)
          len="$(awk -v a="$a" -v b="$b" 'BEGIN { printf "%.3f", b - a }')"
          if awk -v l="$len" -v m="$flat_min" 'BEGIN { exit !(l >= m) }'; then
            warn "空底色帧 ${a}–${b} s（${len} s）：画面上什么都没有"
          else
            short_flat=$((short_flat + 1))
          fi ;;
      esac
    done <"$tmp/runs.txt"
    [ "$black_runs" -gt 0 ] || ok "没有黑帧（扫描 ${scanned:-?} 帧）"
    [ "$short_flat" -eq 0 ] || info "$short_flat 段短于 ${flat_min} s 的空底色帧（转场间隙）"
    if awk '$1 == "flat" || $1 == "black" { s += $3 - $2 } END { exit !(s > 0) }' "$tmp/runs.txt" \
      && within "$(awk '$1 == "flat" || $1 == "black" { s += $3 - $2 } END { printf "%.3f", s }' "$tmp/runs.txt")" "$dur" 0.05; then
      fail "整片都是空底色 / 黑帧：内容没有渲染出来"
    fi

    # Frozen stretches (freezedetect logs start / duration / end; the last may run to EOF).
    freezes="$(awk -v d="$dur" '
      /freeze_start:/ { s = $NF + 0; open = 1 }
      /freeze_end:/ { if (open) printf "%.2f %.2f\n", s, $NF + 0; open = 0 }
      END { if (open) printf "%.2f %.2f\n", s, d }' "$scan_log")"
    if [ -z "$freezes" ]; then
      ok "没有超过 ${freeze_min} s 的静止段"
    else
      while read -r a b; do
        len="$(awk -v a="$a" -v b="$b" 'BEGIN { printf "%.2f", b - a }')"
        if awk -v a="$a" 'BEGIN { exit !(a < 0.1) }' && within "$b" "$dur" 0.1; then
          warn "整片静止 ${a}–${b} s：没有任何动画"
        elif within "$b" "$dur" 0.1; then
          limit="$(awk -v d="$dur" 'BEGIN { l = d * 0.3; if (l < 3) l = 3; printf "%.2f", l }')"
          if awk -v l="$len" -v m="$limit" 'BEGIN { exit !(l > m) }'; then
            warn "片尾静止 ${a}–${b} s（${len} s，超过 ${limit} s）：动画是不是提前结束了？"
          else
            info "片尾静止 ${a}–${b} s（${len} s，结尾定格）"
          fi
        else
          warn "画面静止 ${a}–${b} s（${len} s）：确认是故意的停顿"
        fi
      done <<<"$freezes"
    fi
  fi
fi

# ---------- 3. frames ----------
say ""
say "[3] 抽帧 → $frames_dir"
frame_step="$(awk -v f="$fps" 'BEGIN { printf "%.4f", (f > 0) ? 1 / f : 0.04 }')"
if [ -n "$at" ]; then
  times="$(printf '%s\n' "$at" | tr ',' '\n' | awk 'NF')"
else
  times="$(awk -v d="$dur" -v n="$count" 'BEGIN {
    a = (d > 2) ? 0.5 : d * 0.1; b = (d > 2) ? d - 0.5 : d * 0.9
    for (i = 0; i < n; i++) printf "%.2f\n", (n == 1) ? (a + b) / 2 : a + (b - a) * i / (n - 1)
  }')"
fi

shots=()
while read -r t; do
  [ -n "$t" ] || continue
  if ! is_number "$t"; then warn "跳过非法时间点 $t"; continue; fi
  if awk -v t="$t" -v d="$dur" 'BEGIN { exit !(t >= d) }'; then
    warn "时间点 $t s 超出片长 $dur s，改抽最后一帧"
    t="$(awk -v d="$dur" -v s="$frame_step" 'BEGIN { printf "%.3f", d - s }')"
  fi
  out="$frames_dir/$(printf 't%06.2fs.png' "$t")"
  if ! ffmpeg -v error -y -ss "$t" -i "$video" -frames:v 1 "$out" 2>"$tmp/extract.log" || [ ! -s "$out" ]; then
    fail "抽帧失败 @ ${t} s：$(tail -1 "$tmp/extract.log")"
    continue
  fi
  shots+=("$out")
  read -r yavg ymin ymax < <(ffmpeg -v error -i "$out" -vf "signalstats,metadata=mode=print:file=-" -f null - 2>/dev/null |
    awk -F= '/YAVG=/ { a = $2 } /YMIN=/ { lo = $2 } /YMAX=/ { hi = $2 } END { printf "%.1f %d %d\n", a, lo, hi }')
  note=""
  if [ "${ymax:-0}" -lt 24 ]; then note="  ← 黑帧"
  elif [ $((ymax - ymin)) -lt 12 ]; then note="  ← 空底色"
  fi
  [ -z "$note" ] || warns=$((warns + 1))
  say "  $(printf '%7.2f s' "$t")  亮度均值 $(printf '%5.1f' "$yavg")  范围 $(printf '%3d–%3d' "$ymin" "$ymax")  $(basename "$out")$note"
done <<<"$times"

# ---------- 4. contact sheet ----------
if [ ${#shots[@]} -gt 0 ]; then
  n=${#shots[@]}
  cols=4
  [ "$n" -ge "$cols" ] || cols=$n
  rows=$(((n + cols - 1) / cols))
  list="$tmp/frames.txt"
  : >"$list"
  for f in "${shots[@]}"; do
    printf "file '%s'\n" "$(printf '%s' "$f" | sed "s/'/'\\\\''/g")" >>"$list"
  done
  sheet="$frames_dir/contact-sheet.png"
  if ffmpeg -v error -y -f concat -safe 0 -i "$list" \
    -vf "scale=480:-2,pad=iw+4:ih+4:2:2:white,tile=${cols}x${rows}:color=white" -frames:v 1 "$sheet" 2>"$tmp/sheet.log" && [ -s "$sheet" ]; then
    say "  总览图 $(basename "$sheet")（${cols}×${rows}，按时间从左到右、从上到下）"
  else
    warn "总览图生成失败：$(tail -1 "$tmp/sheet.log")"
  fi
fi

# ---------- summary ----------
say ""
if [ "$fails" -gt 0 ]; then
  say "结果：不通过（$fails 项失败，$warns 条警告）"
elif [ "$warns" -gt 0 ]; then
  if [ "$strict" -eq 1 ]; then
    say "结果：不通过（--strict，$warns 条警告）"
  else
    say "结果：通过，$warns 条警告需要人看一眼"
  fi
else
  say "结果：通过"
fi
say "下一步：逐张打开 $frames_dir 里的抽帧，看文字溢出、重叠、乱码、黑帧、动画提前结束。"
say "报告已写入 $report"

if [ "$fails" -gt 0 ] || { [ "$strict" -eq 1 ] && [ "$warns" -gt 0 ]; }; then
  exit 1
fi
exit 0
