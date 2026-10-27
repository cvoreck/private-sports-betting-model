Replace the matching files in sports_betting_cloud/ on GitHub. Keep analytics.py and other files unchanged.
The dashboard has an illustrative preview in Today’s Top 5 when no picks exist. This does NOT create or publish real picks.
To attempt a live run: GitHub Actions > Daily Top 5 > Run workflow. The current publisher requires rows in verified_events and model_predictions. No automated model or eligibility feed is included.
The existing schedule is defined in .github/workflows/daily-top5.yml, which was not supplied here. Confirm its cron separately.
