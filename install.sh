#!/bin/sh
# gitfurl install script: downloads gitfurl's single-file Linux program from a GitHub
# release, checks it against the release's SHA256SUMS, and installs it in your own bin
# folder (or, with --run, runs it once from a temporary folder). gitfurl itself never goes
# online; this script is the one part that does, and only when you run it.
#
# Read it before you run it:
#   curl -fsSL https://raw.githubusercontent.com/undecuple/gitfurl/main/install.sh -o install.sh
#   less install.sh
#   sh install.sh --help
#
# It never uses sudo, refuses to run as root unless --allow-root is given, never touches
# ~/.wine, and installs nothing or runs nothing unless the checksum matches.

set -u

RELEASES=https://github.com/undecuple/gitfurl/releases

usage() {
	cat <<'EOF'
Usage: sh install.sh [OPTIONS]
       sh install.sh [OPTIONS] --run [GITFURL ARGUMENTS...]

Downloads gitfurl's single-file Linux program (x86_64) from a GitHub release, checks it
against the release's SHA256SUMS and installs it as ~/.local/bin/gitfurl (or
$XDG_BIN_HOME/gitfurl). Nothing is installed or run if the checksum does not match.

Options:
  --version vX.Y.Z  use that release instead of the latest one
  --dir DIR         install into DIR instead of ${XDG_BIN_HOME:-$HOME/.local/bin}
  --desktop-entry   after installing, run 'gitfurl cli desktop-entry --yes' (adds gitfurl
                    to your application menu; it prints its plan)
  --uninstall       remove DIR/gitfurl (and its menu entry, through
                    'gitfurl cli desktop-entry --remove --yes', when it has that command)
  --run ARGS...     download and check the program into a temporary folder, run
                    'gitfurl ARGS...' from there, then delete the folder; nothing is
                    installed. Every argument after --run goes to gitfurl unchanged.
  --dry-run         print the URLs, the target and the steps; download nothing
  --allow-root      run even as root (not recommended)
  -h, --help        show this help

Environment:
  GITFURL_BASE_URL  the releases URL to download from (default
                    https://github.com/undecuple/gitfurl/releases); https:// or file://
  XDG_BIN_HOME      the default install folder, if set
  TMPDIR            where the temporary folder is made (default /tmp)

Examples:
  curl -fsSL https://raw.githubusercontent.com/undecuple/gitfurl/main/install.sh | sh
  curl -fsSL https://raw.githubusercontent.com/undecuple/gitfurl/main/install.sh | sh -s -- --run cli install ~/setup.exe --dry-run
  sh install.sh --uninstall
EOF
}

# Status lines go to stdout, except with --run, where stdout belongs to gitfurl.
info() {
	if [ "$mode" = run ]; then
		printf '%s\n' "$*" >&2
	else
		printf '%s\n' "$*"
	fi
}
warn() { printf 'gitfurl install: warning: %s\n' "$*" >&2; }
die() {
	printf 'gitfurl install: error: %s\n' "$*" >&2
	exit 1
}
usage_error() {
	printf 'gitfurl install: error: %s\n' "$*" >&2
	printf "Run 'sh install.sh --help' for the options.\n" >&2
	exit 2
}

# One argument, quoted for a shell, for display only.
quote() {
	case $1 in
	'' | *[!A-Za-z0-9_./:=@%+,-]*)
		printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
		;;
	*) printf '%s' "$1" ;;
	esac
}
quote_all() {
	out=
	for a in "$@"; do
		out="$out $(quote "$a")"
	done
	printf '%s' "${out# }"
}

tmp=
part=
# shellcheck disable=SC2329 # called by the EXIT trap
cleanup() {
	if [ -n "$part" ] && [ -f "$part" ]; then
		rm -f -- "$part"
	fi
	if [ -n "$tmp" ] && [ -d "$tmp" ]; then
		case $tmp in
		"$tmp_parent"/gitfurl-install.*) rm -rf -- "$tmp" ;;
		esac
	fi
}

# Is the script being read from stdin (curl ... | sh, or sh -s)? Then stdin is the
# script's pipe, not something meant for gitfurl.
script_on_stdin() {
	case ${0##*/} in
	sh | -sh | dash | -dash | bash | -bash | ash | -ash | ksh | mksh | yash | posh | zsh | busybox) return 0 ;;
	esac
	return 1
}

fetch() { # URL OUTPUT
	case $1 in
	file:///*)
		cp -- "${1#file://}" "$2" 2>/dev/null || die "cannot read $1"
		;;
	https://*)
		if command -v curl >/dev/null 2>&1; then
			curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 -o "$2" "$1" ||
				die "download failed: $1"
		elif command -v wget >/dev/null 2>&1; then
			wget -q --https-only --secure-protocol=TLSv1_2 -O "$2" "$1" ||
				die "download failed: $1"
		else
			die "neither curl nor wget was found; install one of them, or download the files by hand (see the README)"
		fi
		;;
	*) die "refusing to download $1: only https:// (or file:///) URLs are used" ;;
	esac
}

check_platform() {
	os=$(uname -s) || die "cannot run uname"
	arch=$(uname -m) || die "cannot run uname"
	if [ "$os" != Linux ] || [ "$arch" != x86_64 ]; then
		die "gitfurl's release program is for Linux x86_64 only; this is $os $arch. Run gitfurl from source instead (see the README)."
	fi
}

check_user() {
	uid=$(id -u) || die "cannot run id"
	if [ "$uid" = 0 ] && [ "$allow_root" = 0 ]; then
		die "refusing to run as root: gitfurl installs into your own home and needs no root. Run this as your own user (no sudo), or add --allow-root if you really mean it."
	fi
}

# Reads $tmp/SHA256SUMS and sets asset (the program's file name) and sum (its sha256).
pick_asset() {
	asset=
	sum=
	count=0
	cr=$(printf '\r')
	while IFS= read -r line || [ -n "$line" ]; do
		line=${line%"$cr"}
		h=${line%% *}
		n=${line#* }
		n=${n#[ *]}
		if [ -n "$version" ]; then
			[ "$n" = "gitfurl-$version-linux-x86_64" ] || continue
		else
			case $n in
			gitfurl-v*-linux-x86_64) ;;
			*) continue ;;
			esac
		fi
		count=$((count + 1))
		asset=$n
		sum=$h
	done <"$tmp/SHA256SUMS"
	[ "$count" -ge 1 ] || die "SHA256SUMS ($sums_url) has no line for $want_name. Nothing was installed or run."
	[ "$count" -eq 1 ] || die "SHA256SUMS ($sums_url) has more than one line for $want_name. Nothing was installed or run."
	case $asset in
	*[!A-Za-z0-9._-]*) die "SHA256SUMS names an unexpected file: $(quote "$asset")" ;;
	esac
	case $sum in
	*[!0-9a-fA-F]*) die "SHA256SUMS has a malformed checksum for $asset" ;;
	esac
	[ "${#sum}" -eq 64 ] || die "SHA256SUMS has a malformed checksum for $asset"
}

download_and_verify() {
	tmp=$(mktemp -d "$tmp_parent/gitfurl-install.XXXXXXXX") || die "cannot make a temporary folder in $tmp_parent"
	chmod 0700 "$tmp" || die "cannot set the mode of $tmp"
	info "Downloading $sums_url"
	fetch "$sums_url" "$tmp/SHA256SUMS"
	pick_asset
	info "Downloading $dl_url/$asset"
	fetch "$dl_url/$asset" "$tmp/$asset"
	printf '%s  %s\n' "$sum" "$asset" >"$tmp/SHA256SUMS.asset"
	if ! (cd "$tmp" && sha256sum -c SHA256SUMS.asset) >/dev/null 2>&1; then
		die "$asset does not match its line in SHA256SUMS (expected sha256 $sum). The download is damaged or not the release's file. Nothing was installed or run."
	fi
	info "Checked: $asset matches SHA256SUMS (sha256 $sum)."
	mv -f -- "$tmp/$asset" "$tmp/gitfurl" || die "cannot rename the program in $tmp"
	chmod 0700 "$tmp/gitfurl" || die "cannot set the mode of $tmp/gitfurl"
	prog=$tmp/gitfurl
}

check_target() {
	if [ -L "$target" ]; then
		die "$target is a symbolic link; gitfurl install does not replace or remove links. Remove it yourself or choose another --dir."
	fi
	if [ -e "$target" ] && [ ! -f "$target" ]; then
		die "$target exists and is not a regular file; nothing was changed."
	fi
}

dry_run() {
	info "Dry run: nothing is downloaded, installed or run."
	if [ "$mode" = uninstall ]; then
		info "Target: $target"
		info "Steps:"
		info "  1. If $target has the command, run: $(quote "$target") cli desktop-entry --remove --yes"
		info "  2. Remove $target"
		return 0
	fi
	info "Platform: Linux x86_64"
	info "Checksums: $sums_url"
	if [ -n "$version" ]; then
		info "Program:   $dl_url/$want_name"
	else
		info "Program:   $dl_url/$want_name (the exact name is read from SHA256SUMS)"
	fi
	info "Steps:"
	info "  1. Make a private temporary folder (mode 0700) in ${TMPDIR:-/tmp}; it is removed on exit."
	info "  2. Download SHA256SUMS and the program into it."
	info "  3. Check the program with 'sha256sum -c' against its line in SHA256SUMS; stop if it does not match."
	if [ "$mode" = run ]; then
		info "  4. Run from there: gitfurl $(quote_all "$@")"
		info "  5. Remove the temporary folder. Nothing is installed."
	else
		if [ -e "$target" ]; then
			info "  4. Replace $target (mode 0755), through a temporary file and a rename."
		else
			info "  4. Install it as $target (mode 0755), through a temporary file and a rename."
		fi
		if [ "$desktop" = 1 ]; then
			info "  5. Run: $(quote "$target") cli desktop-entry --yes"
			info "  6. Remove the temporary folder."
		else
			info "  5. Remove the temporary folder."
		fi
	fi
}

do_install() {
	mkdir -p -- "$dir" || die "cannot make the folder $dir"
	check_target
	verb=Installed
	[ -e "$target" ] && verb=Replaced
	part=$(mktemp "$dir/.gitfurl.install.XXXXXXXX") || die "cannot write in $dir"
	cp -- "$prog" "$part" || die "cannot write $part"
	chmod 0755 "$part" || die "cannot set the mode of $part"
	mv -f -- "$part" "$target" || die "cannot rename $part to $target"
	part=
	info "$verb $target (mode 0755)."
	case :${PATH:-}: in
	*:"$dir":* | *:"$dir"/:*) ;;
	*) warn "$dir is not on your PATH; run it as $target, or add $dir to PATH." ;;
	esac
	if [ "$desktop" = 1 ]; then
		info "Running: $(quote "$target") cli desktop-entry --yes"
		"$target" cli desktop-entry --yes </dev/null ||
			die "gitfurl was installed, but 'gitfurl cli desktop-entry --yes' failed (see above)."
	fi
}

do_uninstall() {
	check_target
	if [ ! -e "$target" ]; then
		info "Nothing to remove: $target does not exist."
		return 0
	fi
	if [ -x "$target" ] && "$target" cli desktop-entry --help >/dev/null 2>&1 </dev/null; then
		info "Running: $(quote "$target") cli desktop-entry --remove --yes"
		"$target" cli desktop-entry --remove --yes </dev/null ||
			warn "'gitfurl cli desktop-entry --remove --yes' failed; removing the program anyway."
	else
		info "$target has no 'cli desktop-entry' command; no menu entry to remove."
	fi
	rm -f -- "$target" || die "cannot remove $target"
	info "Removed $target."
}

do_run() {
	info "Running: gitfurl $(quote_all "$@")"
	status=0
	if [ -t 0 ] || ! script_on_stdin; then
		"$prog" "$@" || status=$?
	elif (: </dev/tty) 2>/dev/null; then
		"$prog" "$@" </dev/tty || status=$?
	else
		"$prog" "$@" </dev/null || status=$?
	fi
	exit "$status"
}

main() {
	mode=install
	version=
	dir=
	desktop=0
	dry=0
	allow_root=0
	while [ $# -gt 0 ]; do
		case $1 in
		-h | --help)
			usage
			exit 0
			;;
		--version)
			[ $# -ge 2 ] || usage_error "--version needs a value, for example --version v1.4.0"
			version=$2
			shift 2
			;;
		--version=*)
			version=${1#*=}
			shift
			;;
		--dir)
			[ $# -ge 2 ] || usage_error "--dir needs a folder"
			dir=$2
			[ -n "$dir" ] || usage_error "--dir needs a folder"
			shift 2
			;;
		--dir=*)
			dir=${1#*=}
			[ -n "$dir" ] || usage_error "--dir needs a folder"
			shift
			;;
		--desktop-entry)
			desktop=1
			shift
			;;
		--uninstall)
			mode=uninstall
			shift
			;;
		--dry-run)
			dry=1
			shift
			;;
		--allow-root)
			allow_root=1
			shift
			;;
		--run)
			[ "$mode" = install ] || usage_error "--run and --uninstall cannot be combined"
			mode=run
			shift
			break
			;;
		*) usage_error "unknown option: $(quote "$1")" ;;
		esac
	done

	if [ "$mode" = run ]; then
		[ -z "$dir" ] || usage_error "--dir cannot be used with --run (nothing is installed)"
		[ "$desktop" = 0 ] || usage_error "--desktop-entry cannot be used with --run"
	fi
	if [ "$mode" = uninstall ]; then
		[ -z "$version" ] || usage_error "--version cannot be used with --uninstall"
		[ "$desktop" = 0 ] || usage_error "--desktop-entry cannot be used with --uninstall (it removes the menu entry anyway)"
	fi
	if [ -n "$version" ]; then
		case $version in
		v[0-9]*.[0-9]*.[0-9]*) ;;
		*) usage_error "--version wants a release tag such as v1.4.0, not $(quote "$version")" ;;
		esac
		case $version in
		*[!A-Za-z0-9.-]*) usage_error "--version wants a release tag such as v1.4.0, not $(quote "$version")" ;;
		esac
	fi

	check_user

	if [ "$mode" != run ]; then
		if [ -z "$dir" ]; then
			if [ -n "${XDG_BIN_HOME:-}" ]; then
				dir=$XDG_BIN_HOME
			else
				[ -n "${HOME:-}" ] || die "HOME is not set; use --dir DIR"
				dir=$HOME/.local/bin
			fi
		fi
		# A literal ~ that the calling shell did not expand, as in --dir=~/bin.
		# shellcheck disable=SC2088
		case $dir in
		"~") dir=${HOME:?} ;;
		"~/"*) dir=${HOME:?}/${dir#"~/"} ;;
		esac
		case $dir in
		/*) ;;
		*) dir=$(pwd)/$dir ;;
		esac
		while [ "$dir" != / ] && [ "${dir%/}" != "$dir" ]; do
			dir=${dir%/}
		done
		if [ -n "${HOME:-}" ]; then
			case $dir/ in
			"${HOME%/}/.wine/"*) die "refusing to use $dir: gitfurl never touches ~/.wine" ;;
			esac
		fi
		target=$dir/gitfurl
	fi

	if [ "$mode" = uninstall ]; then
		if [ "$dry" = 1 ]; then
			dry_run
			exit 0
		fi
		do_uninstall
		exit 0
	fi

	check_platform

	base=${GITFURL_BASE_URL:-$RELEASES}
	base=${base%/}
	case $base in
	https://* | file:///*) ;;
	http://*) die "refusing plain http:// ($base): use an https:// URL, or file:/// for a local copy" ;;
	*) die "GITFURL_BASE_URL must start with https:// or file:/// (got $(quote "$base"))" ;;
	esac
	if [ -n "$version" ]; then
		dl_url=$base/download/$version
		want_name=gitfurl-$version-linux-x86_64
	else
		dl_url=$base/latest/download
		want_name=gitfurl-vX.Y.Z-linux-x86_64
	fi
	sums_url=$dl_url/SHA256SUMS

	if [ "$dry" = 1 ]; then
		dry_run "$@"
		exit 0
	fi

	tmp_parent=${TMPDIR:-/tmp}
	tmp_parent=${tmp_parent%/}
	trap cleanup EXIT
	trap 'exit 129' HUP
	trap 'exit 130' INT
	trap 'exit 143' TERM
	download_and_verify
	if [ "$mode" = run ]; then
		do_run "$@"
	fi
	do_install
	exit 0
}

main "$@"
