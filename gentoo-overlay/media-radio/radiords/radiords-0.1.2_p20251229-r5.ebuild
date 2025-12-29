# Copyright 2025
# Distributed under the terms of the MIT license

EAPI=8

PYTHON_COMPAT=( python3_{10..13} )
PYTHON_REQ_USE="tk"

inherit python-single-r1

MY_TAG="v0.1.2+20251229-5"

DESCRIPTION="RTL-SDR FM Radio GUI with RDS (Tkinter + GNU Radio)"
HOMEPAGE="https://github.com/pchmielewski1/radiords"
SRC_URI="https://github.com/pchmielewski1/radiords/archive/refs/tags/${MY_TAG}.tar.gz -> ${P}.tar.gz"

LICENSE="MIT"
SLOT="0"
KEYWORDS="~amd64"

REQUIRED_USE="${PYTHON_REQUIRED_USE}"

RDEPEND="
	${PYTHON_DEPS}
	dev-python/numpy[${PYTHON_USEDEP}]
	dev-python/matplotlib[${PYTHON_USEDEP}]
	media-radio/rtl-sdr
	media-sound/sox
	media-sound/lame
	media-sound/flac
	media-sound/alsa-utils
"

S="${WORKDIR}/${PN}-${MY_TAG#v}"

src_install() {
	# Install the application script into a shared, non-writable location.
	exeinto /usr/share/${PN}
	doeexe rtlsdr_fm_radio_gui.py

	# Provide a stable command name.
	dobin "${FILESDIR}/radiords"
}

pkg_postinst() {
	einfo "radiords is installed. Run: radiords"
	einfo
	einfo "Runtime tools required for full functionality (must be in PATH):"
	einfo "  - rtl_fm (usually from media-radio/rtl-sdr)"
	einfo "  - play (from media-sound/sox)"
		einfo "  - lame (MP3 recording; from media-sound/lame)"
		einfo "  - flac (FLAC recording; from media-sound/flac)"
	einfo "  - amixer (from media-sound/alsa-utils)"
	einfo "  - redsea (RDS decoder; package name may vary on Gentoo/overlays)"
	einfo
	einfo "Stereo playback requires GNU Radio + osmosdr (install via Portage if available)."
}
