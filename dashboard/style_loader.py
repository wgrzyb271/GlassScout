"""Apply theme styles and serve backgrounds separately from UI messages."""

from __future__ import annotations

from html import escape
from pathlib import Path
import re

import streamlit as st
from streamlit import runtime

from dashboard.config import (
    DAY_BACKGROUND_VIDEO,
    NIGHT_BACKGROUND_VIDEO,
    SAFARI_BACKGROUND_IMAGE,
    STYLE_FILE,
)


def _is_safari(user_agent: str) -> bool:
    return bool(re.search("Safari", user_agent, re.I)) and not bool(
        re.search("Chrome|Chromium|CriOS|Edg|EdgiOS|OPR|OPiOS|Firefox|FxiOS|Android", user_agent, re.I)
    )


def _media_url(path: Path, mimetype: str, slot: str) -> str:
    if not path.is_file():
        return ""
    # The same manager used by st.video/st.image, with custom background markup.
    # Register on every run: media ownership is per session, so globally caching
    # these URLs would let another tab's disconnect invalidate the background.
    return runtime.get_instance().media_file_mgr.add(str(path), mimetype, slot)


def inject_styles(*, daytime: bool) -> None:
    accent = "#f7ad68" if daytime else "#b49cff"
    accent_rgb = "247,173,104" if daytime else "180,156,255"
    safari = _is_safari(st.context.headers.get("User-Agent", ""))
    video_path = DAY_BACKGROUND_VIDEO if daytime else NIGHT_BACKGROUND_VIDEO
    video = "" if safari else _media_url(video_path, "video/mp4", "background-video")
    image = _media_url(SAFARI_BACKGROUND_IMAGE, "image/jpeg", "background-image")
    css = STYLE_FILE.read_text(encoding="utf-8")
    css = css.replace("__ACCENT__", accent).replace("__ACCENT_RGB__", accent_rgb)
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
    if image:
        st.markdown(
            f'<div class="app-static-bg" style="background-image:url(\'{escape(image, quote=True)}\')" '
            'aria-hidden="true"></div>',
            unsafe_allow_html=True,
        )
    if video:
        st.markdown(
            '<video class="app-video-bg" autoplay muted loop playsinline preload="none" '
            'disablepictureinpicture aria-hidden="true" tabindex="-1">'
            f'<source src="{escape(video, quote=True)}" type="video/mp4">'
            "</video>",
            unsafe_allow_html=True,
        )
