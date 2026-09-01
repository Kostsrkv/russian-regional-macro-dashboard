"""Local entry point for the Russian regional macro dashboard."""

from __future__ import annotations

import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def main() -> None:
    """Start Streamlit while keeping this module safe to import in tooling."""

    import streamlit as st

    from macro_rus.dashboard import render_dashboard

    st.set_page_config(
        page_title="Russian Regional Macro",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    data_root = Path(
        os.environ.get(
            "MACRO_RUS_DATA_DIR",
            PROJECT_ROOT / "data" / "promoted" / "current",
        )
    )
    render_dashboard(data_root)


if __name__ == "__main__":
    main()
