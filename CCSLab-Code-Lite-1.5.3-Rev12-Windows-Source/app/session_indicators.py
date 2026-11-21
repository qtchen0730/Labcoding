"""One animation timer for the project tree and running-task list."""
import tkinter as tk
from time import time
from tasks import canonical


class SessionIndicators:
    def __init__(self, app, parent, listing):
        self.app, self.listing = app, listing
        scale = float(app.root.tk.call("tk", "scaling")) / (96 / 72)
        self.size = max(14, round(13 * scale))
        self.canvas = app.skin(tk.Canvas(parent, width=30, borderwidth=0, highlightthickness=0,
                                        takefocus=False), "field", None)
        self.canvas.place(relx=1, x=-8, y=0, anchor="ne", width=30, relheight=1)
        self.task_canvas = app.skin(tk.Canvas(app.running_task_list.master, width=30, height=1,
                                             borderwidth=0, highlightthickness=0, takefocus=False),
                                    "sidebar_card", None)
        self.task_canvas.pack(side="right", fill="y", before=app.running_task_list)
        self.canvas.bind("<Button-1>", self.click)
        self.task_canvas.bind("<Button-1>", self.click_task)
        self.phase, self.timer, self.closed = 0, None, False
        for event in ("<Configure>", "<Map>", "<<TreeviewOpen>>", "<<TreeviewClose>>"):
            listing.bind(event, lambda _event: app.root.after_idle(self.draw), add="+")
        self.canvas.bind("<Destroy>", lambda _event: self.close(), add="+")

    def click(self, event):
        row = self.app.tree_rows.get(self.listing.identify_row(event.y))
        if row and not row[1]:
            self.app.navigate_session(row[0], create=True)
        elif row:
            self.app.navigate_session(*row)

    def click_task(self, event):
        index = self.app.running_task_list.nearest(event.y)
        rows = self.app.running_task_rows
        if index < len(rows):
            self.app.navigate_session(rows[index].context.project, rows[index].session_id)

    def state(self, path, session):
        task = self.app.tasks.selected(str(self.app.state), path, session)
        if task:
            return task.metadata(), task
        context = self.app.project_contexts.get((str(self.app.state), canonical(path)))
        return (context.history.get(session, {}).get("task", {}) if context else {}), None

    def indicator(self, canvas, y, height, state, approval=False):
        status, colors = state.get("status"), self.app.colors
        size = min(self.size, max(8, height - 3))
        x, y = (30 - size) / 2, y + (height - size) / 2
        box = (x, y, x + size, y + size)
        if status in {"running", "cancelling"}:
            color = colors["accent"] if status == "running" else colors["muted"]
            canvas.create_arc(*box, start=self.phase, extent=270, style="arc", outline=color, width=1.7)
            if approval:
                canvas.create_text(x+size/2, y+size/2, text="!", fill=colors["remove"],
                                   font=("Segoe UI", max(8, round(size/2)), "bold"), tags="approval")
            return True
        if status == "queued":
            canvas.create_oval(*box, outline=colors["muted"], width=1)
            canvas.create_line(x+size/2, y+2, x+size/2, y+size/2, x+size-2, y+size/2,
                               fill=colors["muted"], width=1)
        elif status == "failed":
            canvas.create_text(x+size/2, y+size/2, text="×", fill=colors["remove"])
        elif status == "completed" and time() - (state.get("ended_at") or 0) < 3:
            canvas.create_line(x+1, y+size/2, x+size/3, y+size-2, x+size-1, y+2,
                               fill=colors["add"], width=1.5)
        return False

    def draw(self):
        if self.closed or not self.canvas.winfo_exists():
            return
        self.canvas.delete("all")
        self.task_canvas.delete("all")
        pending = {task.task_id for task in self.app.pending_approval_tasks()}
        animate = False
        if self.listing.winfo_ismapped():
            for key, (path, session) in self.app.tree_rows.items():
                bounds = self.listing.bbox(key)
                if not bounds or bounds[1] >= self.listing.winfo_height():
                    continue
                if not session:
                    self.canvas.create_text(15, bounds[1]+bounds[3]/2, text="＋", fill=self.app.colors["muted"])
                    continue
                state, task = self.state(path, session)
                animate |= self.indicator(self.canvas, bounds[1], bounds[3], state,
                                          bool(task and task.task_id in pending))
        if self.app.running_task_list.winfo_ismapped():
            for index, task in enumerate(self.app.running_task_rows):
                bounds = self.app.running_task_list.bbox(index)
                if bounds:
                    animate |= self.indicator(self.task_canvas, bounds[1], bounds[3], task.metadata(),
                                              task.task_id in pending)
        if animate and self.timer is None:
            self.timer = self.app.root.after(80, self._tick)
        elif not animate:
            self._cancel()
            if self.listing.winfo_ismapped() and any(
                    task.status == "completed" and task.ended_at and time()-task.ended_at < 3
                    for task in self.app.tasks.tasks.values()):
                self.timer = self.app.root.after(1000, self._tick)

    def _tick(self):
        self.timer = None
        self.phase = (self.phase - 25) % 360
        self.draw()

    def _cancel(self):
        if self.timer is not None:
            try:
                self.app.root.after_cancel(self.timer)
            except tk.TclError:
                pass
            self.timer = None

    def close(self):
        self._cancel()
        self.closed = True
