"""Background analysis worker for the ephys auto-pipeline.

Polls eln_analysis_jobs for pending jobs and runs them headless.
Started as a daemon thread by server.py on startup.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
ANALYSIS_SUITE = Path("/opt/labos/analysis")
UPLOAD_DIR     = Path("/opt/labos/analysis_uploads")
RESULTS_DIR    = Path("/opt/labos/analysis_results")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _con():
    import server
    return server.db_conn()


# ── Headless monkeypatches ─────────────────────────────────────────────────────
def _apply_patches():
    """Suppress all GUI and Agg-backend matplotlib calls before importing analysis."""
    import types

    # Matplotlib → Agg backend (no display needed)
    import matplotlib
    matplotlib.use("Agg")

    # Suppress tkinter entirely
    tk_mod = types.ModuleType("tkinter")
    class _FakeTk:
        def __init__(self, *a, **kw): pass
        def withdraw(self): pass
        def destroy(self): pass
    tk_mod.Tk = _FakeTk
    tk_mod.messagebox = types.SimpleNamespace(
        askyesno=lambda *a, **kw: False,   # auto-exclude cap-drift cells
        showinfo=lambda *a, **kw: None,
        showerror=lambda *a, **kw: None,
    )
    sys.modules.setdefault("tkinter", tk_mod)
    sys.modules.setdefault("tkinter.messagebox", tk_mod.messagebox)


# ── Auto test-pulse mode detection ────────────────────────────────────────────
def _detect_protocol_mode(abf_dir: Path) -> str:
    """Return 'Test Pulse First' or 'Test Pulse Later' by inspecting sweepC."""
    try:
        import pyabf
        import numpy as np

        first_times, later_times = [], []
        for f in sorted(abf_dir.glob("*.abf"))[:6]:   # sample first 6 files
            try:
                abf = pyabf.ABF(str(f))
                abf.setSweep(0, channel=1)
                cmd = abf.sweepC
                t   = abf.sweepX
                diff = np.abs(np.diff(cmd))
                peak_t = float(t[np.argmax(diff)])
                (first_times if peak_t < 0.5 else later_times).append(peak_t)
            except Exception:
                continue

        if len(first_times) >= len(later_times):
            return "Test Pulse First"
        return "Test Pulse Later"
    except Exception:
        return "Test Pulse First"   # safe default


# ── Email helper ──────────────────────────────────────────────────────────────
def _send_report_email(
    to_addr: str,
    subject: str,
    body: str,
    attachments: list[Path],
):
    """Send a plain-text email with optional file attachments via SMTP."""
    try:
        import smtplib
        from email.mime.application import MIMEApplication
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        smtp_host = os.environ.get("LABOS_SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.environ.get("LABOS_SMTP_PORT", "587"))
        smtp_user = os.environ.get("LABOS_SMTP_USER", "")
        smtp_pass = os.environ.get("LABOS_SMTP_PASS", "")
        from_addr = os.environ.get("LABOS_SMTP_FROM", smtp_user)

        msg = MIMEMultipart()
        msg["From"]    = from_addr
        msg["To"]      = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        for path in attachments:
            if path.exists():
                with open(path, "rb") as fh:
                    part = MIMEApplication(fh.read(), Name=path.name)
                part["Content-Disposition"] = f'attachment; filename="{path.name}"'
                msg.attach(part)

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        return True
    except Exception as e:
        print(f"[analysis_worker] Email failed: {e}", flush=True)
        return False


# ── LTD analysis runner ───────────────────────────────────────────────────────
def _run_ltd(params: dict, job_id: str) -> dict:
    """Run the LTD analysis pipeline. Returns result dict."""

    upload_path   = UPLOAD_DIR / job_id
    results_path  = RESULTS_DIR / job_id
    results_path.mkdir(parents=True, exist_ok=True)

    # Auto-detect protocol mode unless user specified
    protocol_mode = params.get("protocol_mode") or _detect_protocol_mode(upload_path)

    exp_condition = params.get("exp_condition", "")
    note_date     = params.get("note_date", "")
    prefix        = params.get("prefix", "LTD")
    epsc_min      = float(params.get("epsc_min", 20.0))
    epsc_max      = float(params.get("epsc_max", 1000.0))
    email         = params.get("email", "")
    extra_out     = params.get("extra_out_dir", "")   # user-specified secondary output

    _apply_patches()

    # Add analysis suite to path and import
    suite_str = str(ANALYSIS_SUITE)
    if suite_str not in sys.path:
        sys.path.insert(0, suite_str)

    ltd = importlib.import_module("ltd_analysis_script")

    # Run headless
    ltd.run_analysis_workflow(
        in_dir        = str(upload_path),
        out_dir       = str(results_path),
        prefix        = prefix,
        epsc_min      = epsc_min,
        epsc_max      = epsc_max,
        protocol_mode = protocol_mode,
        exp_condition = exp_condition,
        cap_drift_callback = None,   # auto-exclude (monkeypatched askyesno returns False)
    )

    # Locate output files
    excel_files = sorted(results_path.glob("Summary_LTD*.xlsx"))
    report_files = list(results_path.glob("**/*.png")) + list(results_path.glob("**/*.csv"))

    # Rename Excel to encode condition + date  (fix: was always "Summary_LTD.xlsx")
    renamed_excel = None
    if excel_files:
        src = excel_files[0]
        parts = [p for p in [exp_condition, note_date.replace("-", ""), prefix] if p]
        new_name = f"LTD_Summary_{'_'.join(parts)}.xlsx" if parts else src.name
        dst = results_path / new_name
        if src != dst:
            src.rename(dst)
        renamed_excel = dst
    else:
        renamed_excel = None

    # Copy results to extra output dir if requested
    if extra_out:
        try:
            extra_path = Path(extra_out)
            extra_path.mkdir(parents=True, exist_ok=True)
            if renamed_excel and renamed_excel.exists():
                shutil.copy2(renamed_excel, extra_path / renamed_excel.name)
        except Exception as e:
            print(f"[analysis_worker] Extra copy failed: {e}", flush=True)

    # Count cells from Excel
    n_cells = n_qualified = n_excluded = 0
    if renamed_excel and renamed_excel.exists():
        try:
            import openpyxl
            wb = openpyxl.load_workbook(renamed_excel, read_only=True, data_only=True)
            if "Cell_Classification" in wb.sheetnames:
                ws = wb["Cell_Classification"]
                rows = list(ws.iter_rows(values_only=True))
                header = [str(c).lower() if c else "" for c in (rows[0] if rows else [])]
                m2_idx = next((i for i, h in enumerate(header) if "method2" in h and "class" in h), None)
                for row in rows[1:]:
                    if not any(row):
                        continue
                    n_cells += 1
                    if m2_idx is not None:
                        cls = str(row[m2_idx] or "").lower()
                        if "ltd" in cls or "ltp" in cls or "no change" in cls:
                            n_qualified += 1
                        else:
                            n_excluded += 1
            wb.close()
        except Exception:
            pass

    # Clean up uploaded ABF files immediately
    try:
        shutil.rmtree(upload_path, ignore_errors=True)
    except Exception:
        pass

    result = {
        "protocol_mode": protocol_mode,
        "excel": str(renamed_excel) if renamed_excel else None,
        "results_dir": str(results_path),
        "n_cells": n_cells,
        "n_qualified": n_qualified,
        "n_excluded": n_excluded,
        "exp_condition": exp_condition,
        "note_date": note_date,
    }

    # Send email report
    if email:
        subject = f"LTD Analysis Complete — {exp_condition} {note_date} ({n_cells} cells)"
        body = (
            f"Analysis complete.\n\n"
            f"Condition:      {exp_condition}\n"
            f"Date:           {note_date}\n"
            f"Protocol mode:  {protocol_mode}\n"
            f"Total cells:    {n_cells}\n"
            f"Qualified:      {n_qualified}\n"
            f"Excluded:       {n_excluded}\n\n"
            f"Results saved to:\n  {results_path}\n"
            + (f"  {extra_out}\n" if extra_out else "")
        )
        attachments = [renamed_excel] if renamed_excel else []
        _send_report_email(email, subject, body, attachments)

    return result


# ── Worker loop ───────────────────────────────────────────────────────────────
def _worker_loop():
    print("[analysis_worker] Started.", flush=True)
    while True:
        try:
            _process_pending()
        except Exception:
            traceback.print_exc()
        time.sleep(10)


def _process_pending():
    con = _con()
    con.row_factory = __import__("sqlite3").Row
    rows = con.execute(
        "SELECT * FROM eln_analysis_jobs WHERE status='pending' ORDER BY created_at LIMIT 1"
    ).fetchall()

    for row in rows:
        job_id = row["id"]
        try:
            params = json.loads(row["params"] or "{}")
            con.execute(
                "UPDATE eln_analysis_jobs SET status='running', updated_at=? WHERE id=?",
                [_now(), job_id],
            )
            con.commit()
            print(f"[analysis_worker] Running job {job_id}", flush=True)

            atype = row["analysis_type"]
            if atype == "ltd":
                result = _run_ltd(params, job_id)
            else:
                raise ValueError(f"Unknown analysis_type: {atype}")

            con.execute(
                "UPDATE eln_analysis_jobs SET status='done', result=?, updated_at=? WHERE id=?",
                [json.dumps(result), _now(), job_id],
            )
            con.commit()
            print(f"[analysis_worker] Job {job_id} done.", flush=True)

        except Exception as err:
            traceback.print_exc()
            con.execute(
                "UPDATE eln_analysis_jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                [str(err)[:2000], _now(), job_id],
            )
            con.commit()
            # Clean up uploads on failure too
            try:
                shutil.rmtree(UPLOAD_DIR / job_id, ignore_errors=True)
            except Exception:
                pass


def start_worker():
    """Called from server.py main(). Starts the background worker thread."""
    t = threading.Thread(target=_worker_loop, daemon=True, name="analysis-worker")
    t.start()
    return t
