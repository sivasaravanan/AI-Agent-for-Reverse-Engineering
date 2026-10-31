# Virtual machine windows (App Preview)

App Preview > **🖥 Virtual machines ▾**

| Window | What it is | Open it |
|---|---|---|
| Manager | All machines, Details / Snapshots / Log, live Preview | Virtual machines > Manager... |
| Machine window | Screen or console of one machine; menus Machine / View / Input / Devices / Help; status bar | Manager > Show, or Virtual machines > Machine window |
| Snapshots | Snapshot tree with picture, console, settings | Manager > Snapshots tab, Ctrl+T in the machine window to take one |
| Settings | General, System, Display, Storage, Network, Shared Folders, User Interface | Manager > Settings, Ctrl+E / Ctrl+S |

## Machines
A machine = project + mode (run/debug/trace/test) + start command + settings. One is created for every program you run in App Preview (same project, command and mode = same machine). **New...** in the Manager creates one by hand. App Preview runs one program at a time, so one machine can be running.

**Start** loads the machine into the panel and uses the panel's ordinary Start: the exact plan is shown for approval every time.

## Snapshots
A snapshot stores the screen picture, console text, settings and start command (folder `data/vm/<machine>/<snapshot>/`). It does **not** store the container's memory or disk: runs are disposable containers. **Restore** puts the settings and start command back and shows the recorded picture; the next Start runs the project fresh. Snapshots form a tree: one taken while another is current is its child; deleting a snapshot moves its children up. *Settings > Display > Take a snapshot automatically when the run ends* keeps the last 10 automatic snapshots.

## Keys (machine window)
Ctrl+T take snapshot, Ctrl+Shift+S snapshot tree, Ctrl+E settings, F11 full screen, Esc leave full screen. With **Input > Capture mouse and keyboard** on, clicks and typing go to the program; Ctrl combinations stay with the window.


## Working with the machine (0.9.20)
- The machine has a small **desktop** (Openbox): title bars, move / resize / close, dialogs on top. Rebuild the GUI preview image once (**Prepare GUI preview image**) to get it; the status bar shows 🪟 desktop when it is there.
- **Click the screen and type**: the window captures mouse and keyboard by default (Input menu or the 🖱 icon switch it off). Drag, wheel, right-click and all keys work; F11 = full screen.
- Pictures and input travel over one fast channel; if it cannot start, the slower snapshot path is used automatically.
- It is a container with a virtual screen, not a hypervisor: Linux programs run, a full guest operating system does not.
