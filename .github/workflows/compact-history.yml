name: Compact Historical Data (Manual)

on:
  workflow_dispatch:
    inputs:
      mode:
        description: 'Preview first; migrate only after reviewing results'
        required: true
        type: choice
        default: preview
        options:
          - preview
          - migrate

permissions:
  contents: read

concurrency:
  group: sports-edge-compact-history
  cancel-in-progress: false

jobs:
  compact:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install database driver
        run: python -m pip install "psycopg[binary]"
      - name: Preview or migrate historical data
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          MODE: ${{ inputs.mode }}
        run: |
          if [ "$MODE" = "preview" ]; then
            python sports_betting_cloud/compact_history.py --dry-run
          else
            python sports_betting_cloud/compact_history.py
          fi
