# Private Top 5 — Cloud MVP

A cloud-ready Streamlit betting **ledger and Top 5 dashboard** using a persistent Supabase PostgreSQL database.

**Important:** This MVP does not yet fetch odds, train six statistical models, automatically rank today's picks, verify game results, or generate AI predictions. It is the hosting/storage foundation for those features. Never represent manually entered probabilities as validated model predictions.

## Deploy (GitHub + Supabase + Streamlit Community Cloud)

1. On GitHub create a **private** repository. Upload the **contents of this folder** (app.py at the repo root). Never upload `.env`, `secrets.toml`, database passwords, or local `.db` files.
2. In Supabase create a project, wait for the database to become ready, and open **Connect**. Copy the **Postgres connection string** (prefer the Session Pooler connection string for IPv4-compatible environments; use the connection method recommended by Supabase for your network). Replace the password placeholder with your database password. URL-encode special characters in the password if needed. Ensure SSL is enabled (e.g., `?sslmode=require` if the URL doesn't already contain parameters). Do **not** paste the connection string into GitHub or chat.
3. On https://share.streamlit.io choose **Create app**, your GitHub repo, branch `main`, main file `app.py`. In **Advanced settings → Secrets**, add:

   ```toml
   DATABASE_URL = "postgresql://YOUR_USER:YOUR_PASSWORD@YOUR_HOST:5432/postgres?sslmode=require"
   ```

   Use your actual Supabase-provided URL; the above is only a format example. Never expose it publicly.
4. Deploy. The app creates its own `picks` and `baseline` tables the first time it connects. Check the dashboard, add a test pick, refresh, and confirm the record persists.
5. **Privacy:** In Streamlit Cloud settings configure access restrictions (private app / invited viewers as supported by your account). A private GitHub repository alone does NOT mean the deployed website is private. Do not distribute the app URL until access is restricted. For stronger user authentication, add a real auth layer before entering sensitive data.
6. Export existing local records from your old MVP as CSV and import through the new app. Imports append; verify duplicates manually. Do not double-count any aggregate historical baseline.

## How to use

- **Dashboard:** Running record (W–L–P, net units, ROI), sport records, yesterday's recap, ledger.
- **Top 5 card:** Displays manually entered, sourced ranked picks for the chosen date.
- **Add pick:** Record odds, matchup, selection, source, model probability (only if independently estimated), and optional rank.
- **Grade results:** Manually settle a pick with a verification URL; unresolved results stay pending.
- **Import / backup:** Import old CSV picks and export ledger. A historical aggregate baseline is optional and explicitly marked incomplete.

## Planned next phase

Integrate legitimate sports/odds/injury/AP ranking data sources; develop **six distinct** calibrated sport models (NFL, NBA, MLB, College Football, NHL, College Basketball), moneyline/spread first and props when the evidence supports a strong edge; backtest on out-of-sample games; rank across sports by confidence and expected value; implement scheduled ingestion and verified grading. Free Streamlit hosting can sleep and free Supabase projects can pause; neither provides guaranteed always-on scheduled automation.
