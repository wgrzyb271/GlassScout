"""Load the standalone stylesheet and apply runtime theme values."""

from __future__ import annotations

from base64 import b64encode
from pathlib import Path

import streamlit as st

from dashboard.config import (
    DAY_BACKGROUND_VIDEO,
    NIGHT_BACKGROUND_VIDEO,
    SAFARI_BACKGROUND_IMAGE,
    STYLE_FILE,
)


@st.cache_data(show_spinner=False)
def _read_video(path: str, modified_ns: int) -> str:
    del modified_ns  # File timestamp is part of the cache key.
    try:
        encoded = b64encode(Path(path).read_bytes()).decode("ascii")
        return f"data:video/mp4;base64,{encoded}"
    except OSError:
        return ""


@st.cache_data(show_spinner=False)
def _read_image(path: str, modified_ns: int) -> str:
    del modified_ns  # File timestamp is part of the cache key.
    try:
        encoded = b64encode(Path(path).read_bytes()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except OSError:
        return ""


def inject_styles(*, daytime: bool) -> None:
    accent = "#f7ad68" if daytime else "#b49cff"
    accent_rgb = "247,173,104" if daytime else "180,156,255"
    video_path = DAY_BACKGROUND_VIDEO if daytime else NIGHT_BACKGROUND_VIDEO
    try:
        modified_ns = video_path.stat().st_mtime_ns
    except OSError:
        modified_ns = 0
    video = _read_video(str(video_path), modified_ns)
    try:
        image_modified_ns = SAFARI_BACKGROUND_IMAGE.stat().st_mtime_ns
    except OSError:
        image_modified_ns = 0
    image = _read_image(str(SAFARI_BACKGROUND_IMAGE), image_modified_ns)
    css = STYLE_FILE.read_text(encoding="utf-8")
    css = (css.replace("__ACCENT__", accent)
              .replace("__ACCENT_RGB__", accent_rgb))
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
    if image:
        st.markdown(
            f'<div class="app-static-bg" style="background-image:url(\'{image}\')" '
            'aria-hidden="true"></div>',
            unsafe_allow_html=True,
        )
    if video:
        st.markdown(
            '<video class="app-video-bg" autoplay muted loop playsinline preload="auto" '
            'disablepictureinpicture aria-hidden="true" tabindex="-1">'
            f'<source src="{video}" type="video/mp4">'
            "</video>",
            unsafe_allow_html=True,
        )
    st.iframe(
        """
        <script>
          (() => {
            const ua = navigator.userAgent;
            const safari = /Safari/i.test(ua)
              && !/(Chrome|Chromium|CriOS|Edg|EdgiOS|OPR|OPiOS|Firefox|FxiOS|Android)/i.test(ua);
            window.parent.document.documentElement.classList.toggle('safari-browser', safari);
          })();
        </script>
        """,
        width=1,
        height=1,
        alt="Browser compatibility helper",
    )
