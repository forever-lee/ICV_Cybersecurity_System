#!/usr/bin/env python3
"""Show cloud downlink messages confirmed by the Bluetooth sender."""

import json
import os
import threading
from datetime import datetime
from queue import Queue
from urllib.request import Request, urlopen


def new_confirmed_tx(snapshot, seen):
    """Return each confirmed TX once, oldest first; ignore uplink traffic."""
    bluetooth = snapshot.get("bluetooth") or {}
    records = bluetooth.get("traffic_history") or []
    result = []
    for record in reversed(records):
        if record.get("direction") != "TX" or not record.get("command_id"):
            continue
        key = (record["command_id"], record.get("received_at_ms"))
        if key not in seen:
            seen.add(key)
            result.append(record)
    return result


def fetch_snapshot(url, token):
    headers = {"Cookie": "vcl_access={}".format(token)} if token else {}
    request = Request(url, headers=headers)
    with urlopen(request, timeout=2) as response:
        return json.load(response)


def main():
    import tkinter as tk
    from tkinter import ttk

    port = int(os.getenv("BLUETOOTH_TX_VIEWER_PORT", "8001"))
    vehicle_id = os.getenv("BLUETOOTH_TX_VIEWER_VEHICLE", "VHC-001")
    token = os.getenv("BLUETOOTH_TX_VIEWER_TOKEN", "")
    url = "http://127.0.0.1:{}/api/vehicles/{}/metrics".format(port, vehicle_id)
    events = Queue()
    stopped = threading.Event()

    def poll_cloud():
        seen = set()
        while not stopped.is_set():
            try:
                snapshot = fetch_snapshot(url, token)
                events.put(("status", "已连接云端 · 仅显示笔记本已确认的 TX 报文"))
                for record in new_confirmed_tx(snapshot, seen):
                    events.put(("tx", record))
            except Exception as exc:
                events.put(("status", "等待云端连接：{}".format(exc)))
            stopped.wait(1)

    root = tk.Tk()
    root.title("V-SHIELD · 云端下发报文 TX")
    root.geometry("820x540")
    root.minsize(620, 380)
    root.configure(bg="#091623")

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Tx.TFrame", background="#091623")
    style.configure("Tx.TLabel", background="#091623", foreground="#d8ecf7",
                    font=("Microsoft YaHei UI", 11))
    style.configure("Title.Tx.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
    style.configure("Status.Tx.TLabel", foreground="#48d8be",
                    font=("Microsoft YaHei UI", 10))

    frame = ttk.Frame(root, style="Tx.TFrame", padding=20)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="云端 → 笔记本  TX 报文", style="Title.Tx.TLabel").pack(anchor="w")
    status = tk.StringVar(value="正在连接云端…")
    ttk.Label(frame, textvariable=status, style="Status.Tx.TLabel").pack(anchor="w", pady=(6, 16))
    ttk.Label(frame, text="最新收到的内容", style="Tx.TLabel").pack(anchor="w")
    latest = tk.Text(frame, height=4, wrap="word", font=("Microsoft YaHei UI", 16),
                     bg="#10283b", fg="#f1fbff", insertbackground="#f1fbff",
                     relief="flat", padx=12, pady=10, state="disabled")
    latest.pack(fill="x", pady=(7, 18))
    ttk.Label(frame, text="接收记录（仅 TX）", style="Tx.TLabel").pack(anchor="w")
    history_frame = ttk.Frame(frame, style="Tx.TFrame")
    history_frame.pack(fill="both", expand=True, pady=(7, 0))
    scrollbar = ttk.Scrollbar(history_frame)
    scrollbar.pack(side="right", fill="y")
    history = tk.Text(history_frame, wrap="word", font=("Microsoft YaHei UI", 11),
                      bg="#10283b", fg="#d8ecf7", relief="flat", padx=12, pady=10,
                      state="disabled", yscrollcommand=scrollbar.set)
    history.pack(side="left", fill="both", expand=True)
    scrollbar.config(command=history.yview)

    def show_text(widget, text, replace=False):
        widget.config(state="normal")
        if replace:
            widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.see("end")
        widget.config(state="disabled")

    def consume_events():
        while not events.empty():
            kind, value = events.get_nowait()
            if kind == "status":
                status.set(value)
                continue
            timestamp = datetime.fromtimestamp(
                value["received_at_ms"] / 1000
            ).strftime("%H:%M:%S.%f")[:-3]
            text = value.get("text", "")
            show_text(latest, text, replace=True)
            show_text(history, "{}  TX #{}  {}\nHEX: {}\n\n".format(
                timestamp, value["command_id"], text, value.get("hex", "")))
        if not stopped.is_set():
            root.after(100, consume_events)

    def close():
        stopped.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    threading.Thread(target=poll_cloud, daemon=True).start()
    root.after(100, consume_events)
    root.mainloop()


if __name__ == "__main__":
    main()
