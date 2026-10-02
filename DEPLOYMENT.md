# Deployment guide

## Recommended free host

Use [Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud) for the first public version. It is Streamlit's free hosting service, connects directly to GitHub, and publishes the app at a stable `streamlit.app` URL.

This is appropriate for a research pilot, not a commitment to fixed production capacity. Community Cloud enforces resource limits, and Streamlit does not publish a fixed capacity guarantee for every free app. Keep the published dataset compact, cache deterministic reads, and monitor the app logs. If the app exceeds its available resources, reduce the promoted dataset or move it to an external data service before considering paid hosting.

## Publication boundary

The public deployment must contain only code, configuration, documentation, and datasets that are cleared for redistribution.

- Keep original FNS, Treasury, Rosstat, and regional-budget downloads out of the public GitHub repository unless their redistribution status has been reviewed.
- Keep the immutable raw archive and analyst working files private.
- Publish only derived, disclosure-reviewed files under `data/promoted/current/` or a pinned research release under `data/dashboard/releases/`.
- Include only the columns and regions needed by the dashboard. Remove source-document binaries, temporary conversions, local paths, analyst notes, and personal metadata.
- Preserve provenance in the promoted tables: source agency, source URL, reporting cutoff, publication/retrieval date, source vintage, unit, and quality flags.
- Do not publish a promoted vintage until the validation suite passes and an analyst approves it.
- Never commit `.streamlit/secrets.toml`, API keys, VPN settings, cookies, credentials, or tokens. Community Cloud secrets belong in the app's **Advanced settings**.

The application loads the immutable processed release selected by `data/dashboard/current.json` when no local data-directory overrides are set. The pointer pins the release manifest checksum; every published payload is checked before the dashboard loads. Publication does not promote these research candidates to independently verified data. If there is no public-release pointer, the legacy default is `data/promoted/current/`.

Explicit `MACRO_RUS_DATA_DIR` or `MACRO_RUS_*_CANDIDATE_DIR` overrides retain the private local-review workflow and bypass the public pointer. Community Cloud should use the checked-in pointer with no machine-specific directory overrides. The app does not require raw workbooks, local audit outputs, a VPN, or Russian-site access at runtime.

## Current research release

The 2026-10-02 release preserves the reviewed interface and processed observations for 78 eligible regions. It includes industry PIT, monthly/annual/cumulative industrial production, retained revenues, expenditure functions, social expenditure, fiscal balances and financing. It does not invent unavailable wages, consumer inflation, fuel prices, debt stocks or military-contract spending.

The release remains `candidate_not_promoted`, with missingness, source vintages and plan-reconciliation caveats retained. CSV compression is lossless. Cumulative-production proofs are bounded for cloud distribution; the original private audit hashes and source-cell lineage remain recorded. Raw downloads, analyst documents, private research-exchange packages and the large audit histories are excluded.

To select a future validated release or roll back the data without changing dashboard logic, update only the repository-relative path, release ID and manifest checksum in `data/dashboard/current.json`, then test and publish the new commit. Never overwrite a release directory. An invalid pointer or corrupted payload fails closed instead of silently serving the old pilot data.

## One-time GitHub setup

1. Create a GitHub repository. A public repository is simplest for a freely accessible dashboard. A private repository can also be connected, but review Streamlit's current repository and viewer permissions before relying on it.
2. Add a `.gitignore` before the first push. At minimum, exclude raw downloads, working data, local secrets, Python caches, virtual environments, notebook checkpoints, editor files, and OS metadata.
3. Commit the application, `requirements.txt`, `pyproject.toml`, tests, this guide, and the disclosure-reviewed `data/promoted/current/` files.
4. Push the deployment commit to `main` and confirm the GitHub Actions test workflow passes.
5. Create or sign in to a Community Cloud account at [share.streamlit.io](https://share.streamlit.io), connect GitHub, and authorize access to the selected repository. The deploying user needs GitHub admin permission for that repository.

Community Cloud starts the app from the repository root, so repository-relative paths should be tested from that same location.

## Pre-deployment check

Run from the repository root with Python 3.12:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pytest
streamlit run app.py
```

Before publishing, verify:

- both pilot regions load without exceptions;
- all pages use the selected region consistently;
- missing observations are shown as missing, never as zero or `nan`;
- displayed source vintages and latest-complete-period labels are correct;
- downloads contain only approved promoted data;
- no raw archives, credentials, local paths, or private notes are tracked by Git;
- the initial app load and common filter changes fit comfortably within the free host's resources.

## Deploy to Community Cloud

1. In Community Cloud, select **Create app** and choose the existing GitHub repository.
2. Set the branch to `main`.
3. Set the entrypoint file to `app.py`.
4. In **Advanced settings**, choose Python **3.12**. Community Cloud currently defaults to Python 3.12, but selecting it explicitly makes the deployment choice auditable.
5. Leave secrets empty for the repository-backed, public-data version. If a future external service requires credentials, enter them in Community Cloud's secrets field and access them through `st.secrets`; do not commit them.
6. Choose an available subdomain and deploy.
7. Inspect the build logs, then perform the smoke checks below at the public URL.

Community Cloud detects GitHub changes and updates the deployed app. Dependency changes can take longer because the environment must reinstall them.

## Post-deployment smoke test

- Open the public URL in a signed-out/private browser window.
- Select Kaluga and then Sverdlovsk; verify all visible metrics, charts, tables, source labels, and downloads update.
- Confirm the app clearly distinguishes plan execution from a validated fiscal shortfall.
- Confirm PIT and industrial-production comparisons use matched scopes and periods.
- Check keyboard navigation, focus visibility, chart table alternatives, contrast, and mobile-width behavior.
- Open **Manage app** and review the logs for missing files, dependency errors, or resource warnings.
- Record the deployed Git commit SHA and promoted-data vintage in the release notes.

## Routine release

1. Build and validate a candidate promoted-data vintage privately.
2. Review the disclosure boundary and analytical quality flags.
3. Replace the files under `data/promoted/current/` in a release branch.
4. Run the complete test suite and local smoke test.
5. Open a pull request showing code changes, data-vintage changes, validation results, and known limitations.
6. Merge only after CI and analyst review pass.
7. Verify the Community Cloud update and record the Git commit SHA and source vintages.

Do not make the deployed app download from VPN-only Russian endpoints. The release workflow is `manual download -> private ingestion -> validation -> analyst approval -> promote compact data -> deploy`.

## Rollback and recovery

The rollback unit is the Git commit: application code and the promoted data snapshot must change together.

1. Identify the last verified commit and its promoted-data vintage.
2. Revert the faulty release with a new Git commit and push it to `main`; do not rewrite shared history.
3. Wait for Community Cloud to update, then repeat the smoke test.
4. If the app does not rebuild or appears to retain bad cached state, use **Reboot** in the Community Cloud workspace or app-management panel. Rebooting temporarily interrupts viewers.
5. If the Python runtime itself must change, note the current repository, branch, entrypoint, subdomain, and secrets; Community Cloud requires deleting and redeploying the app to change Python versions.

## Privacy decision

A public dashboard should use a public app and redistribution-cleared promoted data. If the underlying data or analysis must remain internal, keep the GitHub repository and app private and explicitly grant viewer access. Do not assume that making the app private makes it acceptable to upload data whose handling rules prohibit third-party cloud storage.

## Official references

- [Streamlit Community Cloud overview](https://docs.streamlit.io/deploy/streamlit-community-cloud)
- [Deploy an app](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [File organization](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/file-organization)
- [App dependencies](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)
- [Secrets management](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
- [Connect GitHub](https://docs.streamlit.io/deploy/streamlit-community-cloud/get-started/connect-your-github-account)
- [Sharing and privacy](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app)
- [Status and limitations](https://docs.streamlit.io/deploy/streamlit-community-cloud/status)
- [Resource-limit troubleshooting](https://docs.streamlit.io/knowledge-base/deploy/resource-limits)
- [Reboot an app](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/reboot-your-app)
- [Upgrade the Python version](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/upgrade-python)
