# Duplicates Manager

This Python application handles duplicated files previously listed by the [find_duplicates.sh](https://github.com/ansalazadevops/duplicates) BASH script.

## Features
- Group the duplicate files by hash.
- Filter by path or filename.
- Checkbox to show the files handled so far.
- Choices for removing files:
    - Temporarily, by sending those to the default trash.
    - Permanently.
    - Individually or in bulk selections.
- Set paging by group numbers: 12, 25 or 50 groups per page.
- Show data summary with the information below:
    - Number of open groups.
    - Amount of files in report.
    - Reclaimable space in GB
    - Total number of handled files by session.
    - Trash file path: `/home/$USER/.duplicates_manager_trash` by default.

## Prerequisites
- Download, install, and configure [Python](https://www.python.org/downloads/) on your local machine.
- Create the file duplicates report using the [find_duplicates.sh](https://github.com/ansalazadevops/duplicates) shell script.


## Running the application

1. Set up the Python virtual environment.

```bash
python -m venv .venv
source .venv/bin/activate
```

2. Install the libraries needed.

```bash
pip install -r requirements.txt
```

3. Start the application with **Python**, **Flask CLI**, or **Gunicorn**.

- Start the application from the terminal with **Python**.

```bash
python duplicates_manager.py `/tmp/file_duplicates_YYYY-MM-DD.out`
```

- Start the application with **Flask CLI**.

```bash
export FLASK_APP=duplicates_manager
export DUPE_REPORT=/tmp/file_duplicates_YYYY-MM-DD.out
export DUPE_TRASH=~/.duplicates_manager_trash

flask run --host 127.0.0.1 --port 8000
```

> *Replace `YYYY-MM-DD` with the datestamp from your report.*

- Start the application with **Gunicorn**.

```bash
export DUPE_REPORT=/tmp/file_duplicates_YYYY-MM-DD.out
export DUPE_TRASH=~/.duplicates_manager_trash

gunicorn --workers 1 --bind 127.0.0.1:8000 duplicates_manager:app --daemon
```

> *Replace `YYYY-MM-DD` with the datestamp from your report.*

 4. Validate the application is running by monitoring the process.

```bash
lsof -i :8000
```

5. Open a web browser using the address `127.0.0.1:8000`.

> *Enjoy the application.*

6. Stop the application when done.

> For **Python** and **FLASK CLI** type `CTRL + C` in the terminal's active session.

> For **Gunicorn**, use the kill process below.

```bash
pkill gunicorn
```
7. Validate that the process is no longer running.

```bash
lsof -i :8000
```

## Conclusion

The `duplicates_manager.py` application is a simple method for handling file duplicates allocated on your local machine.

