"""Load the standalone stylesheet and apply runtime theme values."""

from __future__ import annotations

from base64 import b64encode
from pathlib import Path

import streamlit as st

from dashboard.config import BACKGROUND_IMAGE, STYLE_FILE


@st.cache_data(show_spinner=False)
def _read_background(path: str) -> str:
    try:
        encoded = b64encode(Path(path).read_bytes()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except OSError:
        return ""


def inject_styles(*, daytime: bool) -> None:
    accent = "#f7ad68" if daytime else "#b49cff"
    accent_rgb = "247,173,104" if daytime else "180,156,255"
    image = _read_background(str(BACKGROUND_IMAGE))
    backdrop = f"url('{image}')" if image else "none"
    css = STYLE_FILE.read_text(encoding="utf-8")
    css = (css.replace("__ACCENT__", accent)
              .replace("__ACCENT_RGB__", accent_rgb)
              .replace("__BACKDROP_LAYER__", backdrop))
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
