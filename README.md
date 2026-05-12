# ScholarReader

A small Python tool that reads one Google Scholar author profile, saves it as
`scholar_profile.json`, and optionally shows the data in a Tkinter GUI.

The default Google Scholar user ID is `JbrDaPAAAAAJ`. You can change it with the
`--user-id` command-line option or the `SCHOLAR_USER_ID` environment variable.

## JSON Output

The profile is saved to the fixed file:

```text
scholar_profile.json
```

The file includes:

- `schema_version`
- `date`
- `retrieved_at`
- `user_id`
- `profile_url`
- author `name`
- total `citations`
- `h_index`
- `i10_index`
- up to 10 articles with `title`, `citations`, and `year`

This fixed filename is meant to be consumed by another app, such as an ESP32
program reading the raw JSON from GitHub.

## Local Usage

Use this from Anaconda Prompt, or from a VS Code terminal where your Anaconda
`base` environment is active. The script only uses Python's standard library, so
you do not need to install extra packages.

Open the GUI:

```powershell
python .\scholar_reader.py
```

Use another Scholar user ID:

```powershell
python .\scholar_reader.py --user-id JbrDaPAAAAAJ
```

Refresh only when the saved data is older than one week:

```powershell
python .\scholar_reader.py --max-age-days 7
```

Force a new request and update `scholar_profile.json`:

```powershell
python .\scholar_reader.py --no-gui --force-refresh
```

## GitHub Actions

The workflow at `.github/workflows/update-scholar-profile.yml` runs once per
week and can also be started manually from the GitHub Actions tab.

It runs:

```bash
python scholar_reader.py --no-gui --force-refresh --profile-file scholar_profile.json
```

Then it commits `scholar_profile.json` back to the repository if the file changed.

To use a different Scholar profile without editing the workflow, add a repository
variable named `SCHOLAR_USER_ID` in GitHub:

```text
Settings -> Secrets and variables -> Actions -> Variables
```

If the repository is public, the JSON can later be read from:

```text
https://raw.githubusercontent.com/<your-user>/<your-repo>/<branch>/scholar_profile.json
```

Google Scholar may block automated requests if they are too frequent. The weekly
schedule keeps normal usage low.
