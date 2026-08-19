from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .pipeline import ProcessOptions, process_capture


def _is_non_empty(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return any(path.iterdir())
    except OSError:
        return True


def _capture_label(path: Path) -> str:
    name = path.name
    lower = name.lower()
    for suffix in (".roomcap.zip", ".roomcap", ".zip"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _next_output_dir(capture: Path) -> Path:
    """Choose a new sibling directory without touching an existing result."""
    base = capture.parent / f"{_capture_label(capture)}-RoomTrace"
    candidate = base
    index = 2
    while _is_non_empty(candidate):
        candidate = capture.parent / f"{base.name}-{index}"
        index += 1
    return candidate


def _open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _find_blender() -> Path | None:
    executable = shutil.which("blender")
    if executable:
        return Path(executable)
    if sys.platform.startswith("win"):
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        root = Path(program_files) / "Blender Foundation"
        candidates = sorted(root.glob("Blender */blender.exe"), reverse=True)
        if candidates:
            return candidates[0]
    return None


def _blender_helper() -> Path:
    # Editable installs keep the repository layout. The launcher created by
    # RoomTrace.cmd uses exactly that layout on Windows.
    return Path(__file__).resolve().parents[3] / "blender" / "roomtrace_blender.py"


def launch(initial_capture: Path | None = None) -> None:
    root = tk.Tk()
    root.title("RoomTrace")
    root.geometry("780x520")
    root.minsize(680, 430)

    capture_var = tk.StringVar(value=str(initial_capture) if initial_capture else "")
    output_var = tk.StringVar(value=_next_output_dir(initial_capture) if initial_capture else "")
    status_var = tk.StringVar(value="撮影データを選択してください")
    output_auto_var = tk.BooleanVar(value=True)
    last_output: Path | None = None

    def set_capture(path: Path) -> None:
        capture_var.set(str(path))
        if output_auto_var.get():
            output_var.set(str(_next_output_dir(path)))
        status_var.set("準備完了。『ローカル再構成』を押してください")
        process_button.configure(state="normal")

    def choose_capture() -> None:
        path = filedialog.askopenfilename(
            title="RoomTraceの撮影ZIPを選択",
            filetypes=[
                ("RoomTrace capture", "*.roomcap.zip *.roomcap *.zip"),
                ("All files", "*.*"),
            ],
        )
        if path:
            set_capture(Path(path).expanduser())
            return
        directory = filedialog.askdirectory(title="RoomTraceの撮影フォルダを選択")
        if directory:
            set_capture(Path(directory).expanduser())

    def choose_output() -> None:
        path = filedialog.askdirectory(title="出力先フォルダを選択")
        if path:
            output_auto_var.set(False)
            output_var.set(path)

    def process() -> None:
        nonlocal last_output
        capture_text = capture_var.get().strip()
        capture = Path(capture_text).expanduser() if capture_text else Path()
        if not capture_text or not capture.exists():
            messagebox.showerror("RoomTrace", "撮影ZIPまたは撮影フォルダを選択してください。")
            return

        output_text = output_var.get().strip()
        output = Path(output_text).expanduser() if output_text else _next_output_dir(capture)
        if _is_non_empty(output):
            if output_auto_var.get():
                output = _next_output_dir(capture)
                output_var.set(str(output))
            else:
                messagebox.showerror(
                    "RoomTrace",
                    "指定した出力先にファイルがあります。空のフォルダを選ぶか、自動出力に戻してください。",
                )
                return

        process_button.configure(state="disabled")
        choose_capture_button.configure(state="disabled")
        choose_output_button.configure(state="disabled")
        open_output_button.configure(state="disabled")
        open_blender_button.configure(state="disabled")
        progress.configure(value=0)
        status_var.set("処理を開始しています…")

        def update_progress(message: str, fraction: float) -> None:
            def apply() -> None:
                progress.configure(value=round(fraction * 100))
                status_var.set(f"{message}（{round(fraction * 100)}%）")

            root.after(0, apply)

        def work() -> None:
            try:
                result = process_capture(
                    capture,
                    ProcessOptions(output_dir=output, verify_checksums=True, force=False),
                    progress=update_progress,
                )

                def finished() -> None:
                    nonlocal last_output
                    last_output = result.output_dir
                    progress.configure(value=100)
                    choose_capture_button.configure(state="normal")
                    choose_output_button.configure(state="normal")
                    process_button.configure(state="normal")
                    open_output_button.configure(state="normal")
                    open_blender_button.configure(state="normal" if _find_blender() else "disabled")
                    status_var.set(
                        f"完了: TSDF {result.summary['raw_triangles']:,} triangles / 出力先: {result.output_dir}"
                    )
                    messagebox.showinfo(
                        "RoomTrace",
                        "ローカルTSDF再構成を完了しました。\n\n"
                        f"出力先:\n{result.output_dir}\n\n"
                        "Clean GLBを先にBlenderへ読み込み、Raw GLBを細部確認に使ってください。",
                    )

                root.after(0, finished)
            except Exception as error:

                def failed() -> None:
                    progress.configure(value=0)
                    choose_capture_button.configure(state="normal")
                    choose_output_button.configure(state="normal")
                    process_button.configure(state="normal")
                    status_var.set(f"失敗: {error}")
                    messagebox.showerror("RoomTrace", str(error))

                root.after(0, failed)

        threading.Thread(target=work, name="RoomTraceGuiProcess", daemon=True).start()

    def open_output() -> None:
        if last_output and last_output.exists():
            _open_path(last_output)

    def open_blender() -> None:
        if not last_output or not last_output.exists():
            return
        blender = _find_blender()
        helper = _blender_helper()
        if blender is None:
            messagebox.showwarning("RoomTrace", "Blenderが見つかりません。先にBlenderをインストールしてください。")
            return
        if not helper.exists():
            messagebox.showerror("RoomTrace", f"Blender補助スクリプトが見つかりません:\n{helper}")
            return
        try:
            subprocess.Popen(
                [str(blender), "--python", str(helper), "--", str(last_output), "--raw"],
                cwd=str(last_output),
            )
        except OSError as error:
            messagebox.showerror("RoomTrace", f"Blenderを起動できませんでした:\n{error}")

    root.columnconfigure(1, weight=1)
    root.rowconfigure(5, weight=1)

    heading = ttk.Label(root, text="RoomTrace", font=("Segoe UI", 18, "bold"))
    heading.grid(row=0, column=0, columnspan=3, padx=18, pady=(18, 4), sticky="w")
    ttk.Label(
        root,
        text="撮影ZIPを選ぶ → PC上でTSDF融合 → Blenderで部屋を作る",
    ).grid(row=1, column=0, columnspan=3, padx=20, pady=(0, 18), sticky="w")

    ttk.Label(root, text="撮影データ").grid(row=2, column=0, padx=18, pady=8, sticky="w")
    ttk.Entry(root, textvariable=capture_var).grid(row=2, column=1, padx=8, pady=8, sticky="ew")
    choose_capture_button = ttk.Button(root, text="選択…", command=choose_capture)
    choose_capture_button.grid(row=2, column=2, padx=18, pady=8)

    ttk.Label(root, text="出力先").grid(row=3, column=0, padx=18, pady=8, sticky="w")
    ttk.Entry(root, textvariable=output_var).grid(row=3, column=1, padx=8, pady=8, sticky="ew")
    choose_output_button = ttk.Button(root, text="変更…", command=choose_output)
    choose_output_button.grid(row=3, column=2, padx=18, pady=8)
    ttk.Checkbutton(
        root,
        text="出力先を自動で決める（撮影データの隣）",
        variable=output_auto_var,
        command=lambda: output_auto_var.get() and capture_var.get() and output_var.set(
            str(_next_output_dir(Path(capture_var.get()).expanduser()))
        ),
    ).grid(row=4, column=1, padx=8, pady=(0, 12), sticky="w")

    process_button = ttk.Button(
        root,
        text="ローカル再構成",
        command=process,
        state="normal" if initial_capture else "disabled",
    )
    process_button.grid(row=5, column=1, padx=8, pady=12, sticky="w")
    progress = ttk.Progressbar(root, mode="determinate", maximum=100)
    progress.grid(row=6, column=0, columnspan=3, padx=18, pady=(0, 10), sticky="ew")
    ttk.Label(root, textvariable=status_var, wraplength=740).grid(
        row=7, column=0, columnspan=3, padx=18, pady=8, sticky="w"
    )

    button_frame = ttk.Frame(root)
    button_frame.grid(row=8, column=0, columnspan=3, padx=18, pady=10, sticky="w")
    open_output_button = ttk.Button(button_frame, text="出力フォルダを開く", command=open_output, state="disabled")
    open_output_button.pack(side="left", padx=(0, 8))
    open_blender_button = ttk.Button(button_frame, text="Blenderで開く", command=open_blender, state="disabled")
    open_blender_button.pack(side="left")
    ttk.Label(
        root,
        text="主な出力: room_reference_clean.glb / room_reference_raw.glb / pointcloud.ply / quality_report.html",
        wraplength=740,
    ).grid(row=9, column=0, columnspan=3, padx=18, pady=(8, 18), sticky="nw")

    if initial_capture:
        root.after(200, process)
    root.mainloop()
