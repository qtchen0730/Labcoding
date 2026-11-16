"""Small vector controls, independent of installed symbol fonts."""
from math import cos, sin, pi, hypot, atan2
from pathlib import Path
import sys
import tkinter as tk


def gear_image(master, foreground, background, size=20):
    """A compact antialiased gear for the text's right-hand compound image."""
    fg = tuple(int(foreground[n:n+2], 16) for n in (1,3,5))
    bg = tuple(int(background[n:n+2], 16) for n in (1,3,5))
    result = tk.PhotoImage(master=master, width=size, height=size)
    for y in range(size):
        for x in range(size):
            hits = 0
            for sy in range(4):
                for sx in range(4):
                    dx, dy = x+(sx+.5)/4-size/2, y+(sy+.5)/4-size/2
                    radius, angle = hypot(dx,dy), atan2(dy,dx)
                    hits += 3.1 <= radius <= 7.0 or (6.5 <= radius <= 9 and abs(sin(4*angle)) < .38)
            if hits:
                color = "#"+"".join(f"{round(a*hits/16+b*(1-hits/16)):02x}" for a,b in zip(fg,bg))
                result.put(color, (x,y))
    return result


def apply_window_icon(root):
    base = Path(__file__).resolve().parent
    assets = base / "assets" if getattr(sys, "frozen", False) else base.parents[1] / ".work" / "assets" / "Rev10"
    icon = assets / "app.ico"
    if icon.is_file():
        root.iconbitmap(str(icon))
        root.iconbitmap(default=str(icon))


class ActionIcon(tk.Canvas):
    def __init__(self, app, parent, kind, command):
        super().__init__(parent, width=28, height=28, borderwidth=0,
                         highlightthickness=1, takefocus=True, cursor="hand2")
        self.kind, self.command = kind, command
        self.enabled = True
        app.skin(self, "panel", None, highlightbackground="panel", highlightcolor="accent")
        self.bind("<Button-1>", self.invoke)
        self.bind("<Return>", self.invoke)
        self.bind("<space>", self.invoke)

    def invoke(self, _event=None):
        if self.enabled:
            self.command()
        return "break"

    def theme(self, colors):
        self.delete("all")
        color = colors["heading"]
        if self.kind == "sun":
            self.create_oval(9, 9, 19, 19, outline=color, width=2)
            for n in range(8):
                angle = n * pi / 4
                self.create_line(14 + 8*cos(angle), 14 + 8*sin(angle),
                                 14 + 12*cos(angle), 14 + 12*sin(angle), fill=color, width=2)
        else:
            self.create_oval(7, 7, 21, 21, outline=color, width=2)
            self.create_oval(11, 11, 17, 17, outline=color, width=2)
            for n in range(8):
                angle = n * pi / 4
                self.create_line(14 + 8*cos(angle), 14 + 8*sin(angle),
                                 14 + 11*cos(angle), 14 + 11*sin(angle), fill=color, width=4)
