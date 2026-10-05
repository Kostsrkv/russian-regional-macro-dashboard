"""Local and Streamlit Cloud entry point for the regional macro dashboard."""

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
    from macro_rus.cloud_release import resolve_dashboard_config

    st.set_page_config(
        page_title="Russian Regional Macro",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    try:
        config = resolve_dashboard_config(PROJECT_ROOT, os.environ)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        st.error(f"Published research dataset unavailable: {exc}")
        return
    if config.release_id:
        st.sidebar.caption(f"Research release: {config.release_id} · not independently verified")
    # Explicit private overrides win; cloud uses only its checksummed release.
    fuel_root = os.environ.get("MACRO_RUS_FUEL_CANDIDATE_DIR") or config.fuel_root
    if fuel_root:
        render_dashboard(*config.render_arguments(), fuel_candidate_root=Path(fuel_root))
    else:
        render_dashboard(*config.render_arguments())


if __name__ == "__main__":
    main()
