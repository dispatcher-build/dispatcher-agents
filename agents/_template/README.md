# About this worker

This worker runs on a simple loop: each of its phases runs on its own
interval, and everything it produces shows up on the Overview tab as records.
Anything it flags with an alert shows as a badge on the canvas until you open it.

## How to use it

- **Start it** with the Start button. It keeps its own pace — the schedule in
  the Configuration tab controls how often it wakes.
- **Talk to it** through the assistant in the Code tab: ask it to change what
  the worker does, adjust settings, or explain its code.
- **Give it a goal** so it (and the assistant) know what you want.

## Making it yours

Edit `main.py` in the Code tab. Each `@phase(every_hours=...)` method is one
job the worker does on that interval: do the work, call `self.record(...)` with
anything worth keeping, and `self.alert(...)` for anything you must see. Add
more phases for more jobs; the first one that is due runs each round.

Add Python packages to `requirements.txt`; they install the next time the
worker starts. After code changes, use Save & restart so the running worker
picks them up. If an edit breaks the worker, Reset worker in the Code tab
returns everything to this installed version (your collected data is kept).

Replace this file with instructions for your worker — it renders on the
Overview tab.
