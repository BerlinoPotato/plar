import json, os, sys, threading, subprocess, shlex, time, signal, html
from dataclasses    import dataclass, field
from typing         import Any, Dict, List, Optional
from PySide6        import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
from pathlib        import Path
os.environ["QT_LOGGING_RULES"] = "qt.qpa.window=false"
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QTextEdit

import warnings
warnings.simplefilter("ignore", UserWarning)

def APP_DIR() -> Path:
    # If bundled by PyInstaller, use the EXE folder; otherwise use project root (parent of msrc)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[1]

CONFIG_FILE = str(APP_DIR() / "msrc" / "tools_config.json")
APP_TITLE   = "PLAR : Python Local App Runner [-_-']"
def APP_ASSET(name: str) -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else APP_DIR()
    return base / "data" / "applogo" / name
APP_ICON_FILE = ("plar.ico")

# ======== class QProcRunner ==============================================================
# =========================================================================================
class QProcRunner(QtCore.QObject):
    lineReady = QtCore.Signal(str)   # stdout/stderr lines
    finished  = QtCore.Signal(int)   # exit code

    def __init__(self, parent=None):
        super().__init__(parent)
        self.p = QtCore.QProcess(self)
        self.p.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.p.readyReadStandardOutput.connect(self._on_ready)
        self.p.finished.connect(self._on_finished)

    def start(self, cmd_list, cwd=None, env: dict | None = None):
        program, args = cmd_list[0], cmd_list[1:]
        if cwd:
            self.p.setWorkingDirectory(str(cwd))
        if env:
            qenv = QtCore.QProcessEnvironment.systemEnvironment()
            for k, v in env.items():
                qenv.insert(str(k), str(v))
            self.p.setProcessEnvironment(qenv)
        self.p.start(program, args)

    def kill(self):
        if self.p.state() != QtCore.QProcess.NotRunning:
            self.p.kill()

    def _on_ready(self):
        data = bytes(self.p.readAllStandardOutput())
        text = data.decode(errors="replace")
        for line in text.splitlines():
            self.lineReady.emit(line)

    def _on_finished(self, code, _status):
        self.finished.emit(int(code))


# ---------- Data Models ----------
# ======== class InputSpec ================================================================
# =========================================================================================
@dataclass
class InputSpec:
    name: str
    type: str = "string"  # string | int | float | file | folder | enum
    label: Optional[str] = None
    default: Optional[Any] = None
    choices: Optional[List[str]] = None  # for enum
    required: bool = False
    readonly: bool = False


# ======== class ToolSpec =================================================================
# =========================================================================================
@dataclass
class ToolSpec:
    name: str
    runner_mode: str = "module"           # module | command
    runner: str = ""                      # "pkg.mod:function" OR command template
    script: Optional[str] = None          # optional helper for command template
    output_dir_optional: bool = False
    inputs: List[InputSpec] = field(default_factory=list)
    notes: Optional[str] = None


# ======== MAIN ===========================================================================
# =========================================================================================
# ---------- Utilities ----------
def load_config(path: str) -> List[ToolSpec]:
    """Load the tools config file. 
    If missing, create a simple default config with one fake tool."""
    if not os.path.exists(path):
        # --- create starter config ---
        default_config = [
            {
                "name": "Sample Tool (Demo)",
                "runner_mode": "command",
                "runner": "{python} -c \"print('Hello from sample tool!')\"",
                "script": None,
                "output_dir_optional": False,
                "inputs": [],
                "notes": "This is a placeholder tool automatically created because config file was missing."
            }
        ]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=2, ensure_ascii=False)
        print(f"[!] Config file not found. Created default at: {path}")

    # --- load normally ---
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    tools: List[ToolSpec] = []
    for t in raw:
        
        inputs = []
        for i in t.get("inputs", []):
            i = dict(i)                      # copy
            i.pop("placeholder", None)       # drop legacy key safely
            inputs.append(InputSpec(**i))

        tools.append(
            ToolSpec(
                name=t.get("name", "Untitled"),
                runner_mode=t.get("runner_mode", "module"),
                runner=t.get("runner", ""),
                script=t.get("script"),
                output_dir_optional=t.get("output_dir_optional", False),
                inputs=inputs,
                notes=t.get("notes")
            )
        )
    return tools

def save_config(path: str, tools: List[ToolSpec]):
    data = []
    for t in tools:
        data.append({
            "name": t.name,
            "runner_mode": t.runner_mode,
            "runner": t.runner,
            "script": t.script,
            "output_dir_optional": False,
            "inputs": [{"name": i.name,
                        "type": i.type,
                        "label": i.label,
                        "default": i.default,
                        "choices": i.choices,
                        "required": i.required,
                        "readonly": i.readonly,
                    } for i in t.inputs],

            "notes": t.notes
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def parse_module_runner(s: str):
    # "pkg.mod:function" -> ("pkg.mod", "function")
    if ":" not in s:
        raise ValueError("Runner must be 'pkg.module:function'")
    mod, func = s.split(":", 1)
    return mod.strip(), func.strip()

# ---------- Runner Thread ----------
# ======== class ProcRunner ===============================================================
# =========================================================================================
class ProcRunner(QtCore.QObject):
    lineReady   = QtCore.Signal(str)
    finished    = QtCore.Signal(int)
    started     = QtCore.Signal()
    error       = QtCore.Signal(str)

    def __init__(self, cmd: List[str], cwd: Optional[str] = None):
        super().__init__()
        self.cmd = cmd
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self._stop = False

    @QtCore.Slot()
    def run(self):
        try:
            self.started.emit()
            self.proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                text=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            for line in self.proc.stdout:
                self.lineReady.emit(line.rstrip("\n"))
                if self._stop:
                    break
            rc = self.proc.wait()
            self.finished.emit(rc)
        except Exception as e:
            self.error.emit(str(e))
            self.finished.emit(-1)

    def stop(self):
        self._stop = True
        try:
            if self.proc:
                if os.name == "nt":
                    # try soft break then kill
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                    time.sleep(0.4)
                    self.proc.kill()
                else:
                    self.proc.terminate()
                    time.sleep(0.5)
                    if self.proc.poll() is None:
                        self.proc.kill()
        except Exception:
            pass

# ---------- Tool Editor Dialog ----------
# ======== class ToolEditor ===============================================================
# =========================================================================================
class ToolEditor(QtWidgets.QDialog):
    def __init__(self, parent=None, tool: Optional[ToolSpec]=None):
        super().__init__(parent)
        self.setWindowTitle("Add / Edit Tool")
        self.setMinimumWidth(750)
        self.tool = tool or ToolSpec(name="New Tool")
        self.inputs_table = QtWidgets.QTableWidget(0, 7)
        self.inputs_table.setHorizontalHeaderLabels(["Name","Type","Label","Default","Choices","Required", "Read Only"])
        # === Style the header ===
        header = self.inputs_table.horizontalHeader()
        header.setStyleSheet("""
            QHeaderView::section {
                background-color: #80ADE4F7 ;   /* #80ADE4F7 light blue highlight */
                color: black;                /* text color */
                font-weight: bold;
                border: 1px solid #ccc;
                padding: 6px;
            }
        """)
        self.inputs_table.horizontalHeader().setStretchLastSection(True)
        self.inputs_table.verticalHeader().setVisible(False)

        # Top fields
        self.name_edit = QtWidgets.QLineEdit(self.tool.name)
        self.mode_cb   = QtWidgets.QComboBox(); self.mode_cb.addItems(["module","command"])
        self.mode_cb.setCurrentText(self.tool.runner_mode)
        self.runner_edit = QtWidgets.QLineEdit(self.tool.runner)
        self.script_edit = QtWidgets.QLineEdit(self.tool.script or "")
        self.script_btn  = QtWidgets.QPushButton("...")
        self.script_btn.clicked.connect(self._pick_script)
        # self.output_opt  = QtWidgets.QCheckBox("Output folder optional"); self.output_opt.setChecked(self.tool.output_dir_optional)
        self.notes_edit  = QtWidgets.QPlainTextEdit(self.tool.notes or "")

        # Layout
        form = QtWidgets.QFormLayout()
        form.addRow("Name:", self.name_edit)
        
        form.addRow("Runner mode:", self.mode_cb)
        form.addRow("Runner:", self.runner_edit)
        scr_row = QtWidgets.QHBoxLayout(); scr_row.addWidget(self.script_edit); scr_row.addWidget(self.script_btn)
        form.addRow("Script:", scr_row)
        # form.addRow(self.output_opt)
        form.addRow("Notes:", self.notes_edit)

        # Inputs section
        input_bar = QtWidgets.QHBoxLayout()
        add_btn = QtWidgets.QPushButton("Add Input")
        remove_btn = QtWidgets.QPushButton("Remove Selected")
        add_btn.clicked.connect(self._add_input_row)
        remove_btn.clicked.connect(self._remove_selected_input_rows)
        input_bar.addWidget(add_btn); input_bar.addWidget(remove_btn); input_bar.addStretch()
        
        # --- inside ToolEditor.__init__ (after input_bar is created) ---
        gen_btn = QtWidgets.QPushButton("Generate Parameter Snippets")
        gen_btn.setToolTip("Build argparse code, sample CLI, runner placeholders, and JSON inputs from the current table.")
        gen_btn.clicked.connect(self._on_generate_snippets)
        input_bar.addWidget(gen_btn)


        btn_box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)

        v = QtWidgets.QVBoxLayout(self)
        v.addLayout(form)
        v.addSpacing(10)
        v.addLayout(input_bar)
        v.addWidget(self.inputs_table)
        v.addWidget(btn_box)

        # load existing inputs
        for i in self.tool.inputs:
            self._add_input_row(i)


    # --- inside ToolEditor class ---
    def _read_inputs_from_table(self) -> List[InputSpec]:
        """Read current rows without committing the dialog."""
        specs: List[InputSpec] = []
        for r in range(self.inputs_table.rowCount()):
            name_w = self.inputs_table.cellWidget(r, 0)
            type_w = self.inputs_table.cellWidget(r, 1)
            label_w = self.inputs_table.cellWidget(r, 2)
            default_w = self.inputs_table.cellWidget(r, 3)
            # choices_w = self.inputs_table.cellWidget(r, 4)
            if not name_w or not type_w:
                continue
            name = name_w.text().strip()
            if not name:
                continue
            itype = type_w.currentText()
            label = (label_w.text().strip() or None) if label_w else None
            default_txt = default_w.text() if default_w else ""
            if default_txt == "":
                default = None
            elif itype == "int":
                try: default = int(default_txt)
                except: default = 0
            elif itype == "float":
                try: default = float(default_txt)
                except: default = 0.0
            else:
                default = default_txt

            # choices_txt = choices_w.text().strip() if choices_w else ""
            # choices = [c.strip() for c in choices_txt.split(",")] if (choices_txt and itype in ("enum","multienum")) else None

            required = self._get_checkbox_checked(r, 5)
            readonly = self._get_checkbox_checked(r, 6)

            specs.append(InputSpec(
                name=name, type=itype, label=label, default=default,
                # choices=choices, required=required, readonly=readonly
                required=required, readonly=readonly
            ))
        return specs
    
    # --- inside ToolEditor class ---
    def _build_snippets(self, specs: List[InputSpec]) -> dict[str, str]:
        def py_type(itype: str) -> str | None:
            return {"string":"str","file":"str","folder":"str","int":"int","float":"float","date":"str", "password":"str" }.get(itype)

        # argparse
        lines = []
        lines.append("import argparse\np = argparse.ArgumentParser()")
        for s in specs:
            flag = f'--{s.name}'
            if s.type == "toggle":
                default = "True" if str(s.default).strip().lower() in ("yes","true","on","1") else "False"
                lines.append(f'p.add_argument("{flag}", action=argparse.BooleanOptionalAction, default={default})')
            elif s.type in ("enum","multienum"):
                # multienum comes in as CSV string by default; keep as str for CLI and parse later
                # choices = f", choices={s.choices}" if s.choices else ""
                default = "" if s.default in (None,"") else f", default={repr(s.default)}"
                # lines.append(f'p.add_argument("{flag}", type=str{choices}{default})')
                lines.append(f'p.add_argument("{flag}", type=str{default})')
            else:
                ty = py_type(s.type)
                ty_part = f", type={ty}" if ty else ""
                default = "" if s.default in (None,"") else f", default={repr(s.default)}"
                required = ", required=True" if s.required else ""
                help_part = f', help="{(s.label or s.name).replace(chr(34), chr(39))}"'
                lines.append(f'p.add_argument("{flag}"{ty_part}{required}{default}{help_part})')
        lines.append("\nargs,_ = p.parse_known_args()\n")
        argparse_block = "\n".join(lines)

        # sample CLI line
        cli_bits = []
        for s in specs:
            f = f"--{s.name}"
            if s.type == "toggle":
                on = str(s.default).strip().lower() in ("yes","true","on","1")
                cli_bits.append(f if on else f"--no-{s.name}")
            else:
                val = s.default
                if val in (None, ""):
                    # show placeholder by type
                    placeholder = {
                        "file":"<path/to/file>",
                        "folder":"<path/to/folder>",
                        "int":"<int>",
                        "float":"<float>",
                        "date":"<YYYY-MM-DD>",
                        "multienum":"<a,b,c>",
                    }.get(s.type, "<value>")
                    cli_bits.append(f'{f} {placeholder}')
                else:
                    cli_bits.append(f'{f} "{val}"' if isinstance(val, str) else f'{f} {val}')
        sample_cli = "python -u your_script.py " + " ".join(cli_bits)
        
            # runner template: {python_u} "{script}" + args
        parts = ['{python_u} "{script}"']
        def needs_quotes(s):  # string-ish types get quotes
            return s in ("string","file","folder","date","enum","multienum", "password")
        for s in specs:
            if s.type == "toggle":
                parts.append(f"{{{s.name}_flag}}")
            else:
                if needs_quotes(s.type):
                    parts.append(f'--{s.name} "{{{s.name}}}"')
                else:
                    parts.append(f'--{s.name} {{{s.name}}}')
        runner_template = " ".join(parts)


        # runner placeholders (matches ToolForm._build_command extras)
        ph_lines = ["# Placeholders available in command templates:"]
        for s in specs:
            ph_lines.append(f"{{{s.name}}}")
            if s.type == "toggle":
                ph_lines.append(f"{{{s.name}_flag}}   # --{s.name} or --no-{s.name}")
                ph_lines.append(f"{{{s.name}_yn}}     # yes/no")
                ph_lines.append(f"{{{s.name}_01}}     # 1/0")
        ph_block = "\n".join(ph_lines)

        # JSON inputs array (handy for config authoring)
        import json as _json
        def jdefault(x): return x
        inputs_json = _json.dumps([{
            "name": s.name,
            "type": s.type,
            "label": s.label or s.name,
            "default": s.default,
            # "choices": s.choices,
            # "placeholder": getattr(s, "placeholder", None),
            "required": s.required,
            "readonly": s.readonly
        } for s in specs], indent=2, ensure_ascii=False, default=jdefault)

        return {
            "Argparse (Python)": argparse_block.strip(),
            "Sample CLI": sample_cli.strip(),
            "Runner template": runner_template.strip(), 
            "Template placeholders": ph_block.strip(),
            "JSON inputs": inputs_json.strip(),
        }


    # --- inside ToolEditor class ---
    def _on_generate_snippets(self):
        specs = self._read_inputs_from_table()
        if not specs:
            QtWidgets.QMessageBox.information(self, "No parameters", "Please add at least one input first.")
            return
        data = self._build_snippets(specs)
        self._show_snippet_dialog(data)

    def _show_snippet_dialog(self, data: dict[str, str]):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Generated Snippets")
        dlg.resize(900, 600)
        tabs = QtWidgets.QTabWidget()
        for title, text in data.items():
            w = QtWidgets.QWidget()
            v = QtWidgets.QVBoxLayout(w)
            edit = QtWidgets.QPlainTextEdit()
            edit.setReadOnly(True)
            edit.setPlainText(text)
            btns = QtWidgets.QHBoxLayout()
            
            copy_btn = QtWidgets.QPushButton("Copy")

            def _copy(checked=False, e=edit):
                QtGui.QGuiApplication.clipboard().setText(e.toPlainText())
                QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "Copied!")

            copy_btn.clicked.connect(_copy)
                        
            
            btns.addStretch(); btns.addWidget(copy_btn)
            v.addWidget(edit); v.addLayout(btns)
            tabs.addTab(w, title)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject); bb.accepted.connect(dlg.accept)
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.addWidget(tabs); lay.addWidget(bb)
        dlg.exec()


    def _pick_python(self):
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select python.exe", "", "Executables (*.exe);;All files (*.*)")
        if fn:
            self.py_edit.setText(fn)

    def _pick_script(self):
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select script", "", "Python (*.py);;All files (*.*)")
        if fn:
            self.script_edit.setText(fn)
            
            
    def _add_input_row(self, spec: Optional[InputSpec] = None):
        r = self.inputs_table.rowCount()
        self.inputs_table.insertRow(r)

		# Name
        name = QtWidgets.QLineEdit(spec.name if spec else "")
        self.inputs_table.setCellWidget(r, 0, name)

		# Type
        type_cb = QtWidgets.QComboBox()
        type_cb.addItems(["string","int","float","file","folder","enum","multienum","toggle","date","list", "password"])

        type_cb.setCurrentText(spec.type if spec else "string")
        self.inputs_table.setCellWidget(r, 1, type_cb)

		# Label
        label = QtWidgets.QLineEdit(spec.label if spec else "")
        self.inputs_table.setCellWidget(r, 2, label)

		# Default
        default = QtWidgets.QLineEdit("" if not spec or spec.default is None else str(spec.default))
        self.inputs_table.setCellWidget(r, 3, default)

		# Choices
        choices = QtWidgets.QLineEdit("" if not spec or not spec.choices else ",".join(spec.choices))
        self.inputs_table.setCellWidget(r, 4, choices)

		# Required (centered)
        req = QtWidgets.QCheckBox()
        req.setChecked(spec.required if spec else False)
        cell_req = QtWidgets.QWidget()
        lay_req = QtWidgets.QHBoxLayout(cell_req); lay_req.setContentsMargins(0,0,0,0)
        lay_req.addStretch(1); lay_req.addWidget(req); lay_req.addStretch(1)
        self.inputs_table.setCellWidget(r, 5, cell_req)

        # Read-only (centered)
        ro = QtWidgets.QCheckBox()
        ro.setChecked(getattr(spec, "readonly", False) if spec else False)
        cell_ro = QtWidgets.QWidget()
        lay_ro = QtWidgets.QHBoxLayout(cell_ro); lay_ro.setContentsMargins(0,0,0,0)
        lay_ro.addStretch(1); lay_ro.addWidget(ro); lay_ro.addStretch(1)
        self.inputs_table.setCellWidget(r, 6, cell_ro)
        
    def _remove_selected_input_rows(self):
        rows = sorted(set([i.row() for i in self.inputs_table.selectedIndexes()]), reverse=True)
        for r in rows:
            self.inputs_table.removeRow(r)

    def _get_required_checked(self, row: int) -> bool:
        cell = self.inputs_table.cellWidget(row, 5)
        if cell is None:
            return False
        layout = cell.layout()
        if layout is None:
            return False
        for i in range(layout.count()):
            w = layout.itemAt(i).widget()
            if isinstance(w, QtWidgets.QCheckBox):
                return w.isChecked()
        return False
    
    def _get_checkbox_checked(self, row: int, col: int) -> bool:
        cell = self.inputs_table.cellWidget(row, col)
        if not cell: return False
        lay = cell.layout()
        if not lay: return False
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if isinstance(w, QtWidgets.QCheckBox):
                return w.isChecked()
        return False


    def result_tool(self) -> ToolSpec:
        # Build ToolSpec from UI
        inputs: List[InputSpec] = []
        
        for r in range(self.inputs_table.rowCount()):
            name = self.inputs_table.cellWidget(r, 0).text().strip()
            if not name:
                continue

            type_cb: QtWidgets.QComboBox = self.inputs_table.cellWidget(r, 1)
            itype = type_cb.currentText()

            label = self.inputs_table.cellWidget(r, 2).text().strip() or None

            default_txt = self.inputs_table.cellWidget(r, 3).text()
            if default_txt == "":
                default = None
            elif itype == "int":
                try: default = int(default_txt)
                except: default = 0
            elif itype == "float":
                try: default = float(default_txt)
                except: default = 0.0
            else:
                default = default_txt

            choices_txt = self.inputs_table.cellWidget(r, 4).text().strip()
            choices = [c.strip() for c in choices_txt.split(",")] if (choices_txt and itype in ("enum","multienum")) else None
            

            # required = self._get_required_checked(r)
            required = self._get_checkbox_checked(r, 5)
            readonly = self._get_checkbox_checked(r, 6)

            inputs.append(InputSpec(
                name=name,
                type=itype,
                label=label,
                default=default,
                choices=choices,
                required=required,
                readonly=readonly
            ))
        t = ToolSpec(
            name=self.name_edit.text().strip() or "Untitled",
            runner_mode=self.mode_cb.currentText(),
            runner=self.runner_edit.text().strip(),
            script=self.script_edit.text().strip() or None,
            # output_dir_optional=False,
            inputs=inputs,
            notes=self.notes_edit.toPlainText().strip() or None
        )
        return t

# ---------- Dynamic Form ----------
# ======== class ToolForm =================================================================
# =========================================================================================
class ToolForm(QtWidgets.QWidget):
    runRequested = QtCore.Signal(dict)  # payload of collected values

    def __init__(self, parent=None):
        super().__init__(parent)
        # ---- 0) Core state & essentials -------------------------------------
        self._init_state()

        # ---- 1) Build UI pieces (modular) -----------------------------------
        title_row  = self._create_title_row() # “No tool selected” + info button
        form_box   = self._create_form_box()  # Scrollable “Application Inputs” box
        logs_panel = self._create_log_view()  # Log label + QPlainTextEdit configured
        self._create_run_controls()        # Run / Stop / Import / Export buttons (+ styles)

        # ---- 2) Combine big panels with a resizable splitter ----------------
        splitter = self._create_io_splitter(form_box, logs_panel)
        self._finalize_layout(title_row, splitter)
        self._wire_actions()
        self.runner: Optional[QProcRunner] = None

    # ========================================================================
    # =============== Helper builders (private methods) =======================
    # ========================================================================

    # 0) Core state & essentials
    def _init_state(self):
        """Initialize model/fields and top-level layout container."""
        self._busy = False
        self.tool: Optional[ToolSpec] = None
        self.fields: Dict[str, QtWidgets.QWidget] = {}

        # frequently referenced widgets
        self.output_dir = QtWidgets.QLineEdit()
        self.log        = QtWidgets.QPlainTextEdit()
        self.status     = QtWidgets.QLabel("Ready")
        self.cwd        = os.getcwd()

        # Root layout of this widget
        self._root_v = QtWidgets.QVBoxLayout(self)
        self._root_v.setContentsMargins(0, 0, 0, 0)

    # 1.a) Buttons row: Run / Stop / Import / Export
    def _create_run_controls(self):
        """Create the bottom button row and style primary/danger buttons."""
        # Keep two logical objects for Run/Stop (reused elsewhere)
        self.run_btn  = QtWidgets.QPushButton("Run")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)

        self.run_btn.setObjectName("Primary")
        self.stop_btn.setObjectName("Danger")
        self.run_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaPlay))
        self.stop_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_BrowserStop))
        self.run_btn.setMinimumWidth(140)
        self.stop_btn.setMinimumWidth(140)

        # Import/Export
        self.import_btn = QtWidgets.QPushButton("Import")
        self.export_btn = QtWidgets.QPushButton("Export")
        self.import_btn.setMinimumWidth(80)
        self.export_btn.setMinimumWidth(80)

        # Styles exactly as before btn_run btn run 
        self.run_btn.setStyleSheet("""
        QPushButton#Primary{
            background:#ADE4F7;
            color:black;
            border:1px solid #38B5E0;
            border-radius:10px;
            padding:9px 15px;
            font-weight:600;
        }               
        
        QPushButton#Primary:hover{ background:#38B5E0;}
        QPushButton#Primary:pressed{ background:#104A91; border:none; border-radius:10px;}
        QPushButton#Primary:disabled{ background:#D7DEDE; color:rgba(255,255,255,0.85); border:none; border-radius:10px;}
        """)
        
        self.stop_btn.setStyleSheet("""
        QPushButton#Danger{
            background:#ED7272; color:black;
            border:none; border-radius:10px;
            padding:10px 16px; font-weight:600;
        }
        QPushButton#Danger:hover{ background:#80ED7272; }
        QPushButton#Danger:pressed{ background:#b91c1c; }
        QPushButton#Danger:disabled{ background:#D7DEDE; color:rgba(255,255,255,0.9); }
        """)

        # Build the row layout and keep a handle for later
        self._btn_row = QtWidgets.QHBoxLayout()
        self._btn_row.addWidget(self.run_btn)
        self._btn_row.addWidget(self.stop_btn)
        self._btn_row.addWidget(self.import_btn)
        self._btn_row.addWidget(self.export_btn)
        self._btn_row.addStretch()

    # 1.b) Logs view (label + QPlainTextEdit)
    def _create_log_view(self) -> QtWidgets.QWidget:
        """Configure the log text box and wrap it with its label into a panel."""
        # Configure QPlainTextEdit (unchanged logic)
        self.log.setObjectName("LogView")
        self.log.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.log.setMinimumHeight(80)
        try:
            mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        except Exception:
            for fam in ["Consolas", "Cascadia Mono", "Courier New", "DejaVu Sans Mono", "Menlo", "Monaco"]:
                if QtGui.QFont(fam).exactMatch():
                    mono = QtGui.QFont(fam)
                    break
            else:
                mono = self.font()
        mono.setPointSize(12)
        self.log.setFont(mono)
        self.log.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        metrics = QtGui.QFontMetricsF(mono)
        self.log.setTabStopDistance(4 * metrics.horizontalAdvance(" "))
        self.log.setReadOnly(True)
        
        self.log.setObjectName("LogView")
        self.log.setStyleSheet("""
            QPlainTextEdit#LogView {
                background: #e9f1f6;      /* edit Application Logs background  e9f1f6 FFFFFF */
                color: #010282;            /* text color */
                selection-background-color: rgba(255,255,255,255);
            }
        """)

        self.log.setAutoFillBackground(True)

        # Bundle label + log into a panel
        panel = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)

        label = QtWidgets.QLabel("  Application Logs:")
        
        label.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)

        v.addWidget(label)
        v.addWidget(self.log)
        return panel

    # 1.c) Scrollable “Application Inputs” box (GroupBox + QScrollArea)
    def _create_form_box(self) -> QtWidgets.QGroupBox:
        """Create the scrollable inputs container and its FormLayout."""
        # Form layout that other code will keep populating
        self.form_layout = QtWidgets.QFormLayout()
        self.form_layout.setLabelAlignment(QtCore.Qt.AlignLeft)
        self.form_layout.setFormAlignment(QtCore.Qt.AlignTop)
        self.form_layout.setHorizontalSpacing(14)
        self.form_layout.setVerticalSpacing(10)
        self.form_layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        # Put the layout on a container widget
        self._form_container = QtWidgets.QWidget()
        self._form_container.setLayout(self.form_layout)

        # Wrap it with a scroll area
        self._form_scroll = QtWidgets.QScrollArea()
        self._form_scroll.setWidget(self._form_container)
        self._form_scroll.setWidgetResizable(True)
        self._form_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._form_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

        # Group box shell and style
        self._form_box = QtWidgets.QGroupBox("Application Inputs:")
        base_css = """
            QGroupBox {
                border: 2px solid #33BEE8F7;
                border-radius: 10px;
                background:#80BEE8F7;
                margin-top: 24px; padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 0px; top: -5px;
                padding: 0 6px;
                font-size: 20px; font-weight: 1200;
                background: transparent;
            }

            /* Checkbox: no stretch background, just the square indicator */
            QCheckBox { background: transparent; }
            QCheckBox::indicator {
                width: 18px; height: 18px;
                border: 1px solid #94A3B8;
                border-radius: 3px;
                background: #ffffff;
                margin: 0 6px 0 6px;
            }
            QCheckBox::indicator:hover   { border-color: #64748B; }
            QCheckBox::indicator:checked { background: #ADE4F7; border-color: #38B5E0; image: none; }
            QCheckBox::indicator:disabled{ background: #E5E7EB; border-color: #CBD5E1; }
        """
        self._form_box.setStyleSheet(base_css)

        lay = QtWidgets.QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._form_scroll)
        self._form_box.setLayout(lay)

        # Size policies: allow width to grow, cap height later via setMaximumHeight
        self._form_container.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        self._form_scroll.setSizePolicy(QtWidgets.QSizePolicy.Expanding,   QtWidgets.QSizePolicy.Expanding)
        self._form_box.setSizePolicy(QtWidgets.QSizePolicy.Expanding,      QtWidgets.QSizePolicy.Maximum)

        # Initial fit to content
        QtCore.QTimer.singleShot(0, self._fit_inputs_height)
        return self._form_box

    
    def _fit_inputs_height(self):
        """
        Cap the inputs box so it never grows beyond its content,
        leaving a small cushion below the last parameter.
        """
        if not getattr(self, "_form_box", None) or not getattr(self, "_form_scroll", None):
            return
        cont = self._form_scroll.widget()
        if not cont:
            return

        # Let Qt recompute size hints first
        cont.adjustSize()

        # Base content height (use whichever is larger: layout vs container)
        lay_hint = self.form_layout.sizeHint().height() if getattr(self, "form_layout", None) else 0
        cont_hint = cont.sizeHint().height()
        content_h = max(lay_hint, cont_hint)

        # Scroll area extras
        fw = self._form_scroll.frameWidth()
        scroll_extra = fw * 2

        # Horizontal scrollbar height (when visible)
        hbar = self._form_scroll.horizontalScrollBar()
        hbar_extra = hbar.sizeHint().height() if (hbar and hbar.isVisible()) else 0

        # GroupBox layout margins (top + bottom)
        gb_layout = self._form_box.layout()
        m = gb_layout.contentsMargins() if gb_layout else QtCore.QMargins(0, 0, 0, 0)
        margins_extra = m.top() + m.bottom()

        # Title area allowance
        title_extra = self._form_box.fontMetrics().height() + 14

        # Add a small cushion so the last row never looks “cut”
        cushion = max(12, self.form_layout.verticalSpacing() + 8)

        wanted = content_h + scroll_extra + hbar_extra + margins_extra + title_extra + cushion

        # Apply cap (still allows shrinking and scrolling on very long forms)
        self._form_box.setMaximumHeight(int(wanted))
        self._form_box.updateGeometry()


    # 1.d) Title row (Left: title label, Right: info button)
    def _create_title_row(self) -> QtWidgets.QHBoxLayout:
        """Create the header row with current tool title and info button."""
        self.tool_title = QtWidgets.QLabel("<b>No tool selected</b>")
        self.tool_title.setObjectName("Heading")
        f = self.tool_title.font()
        f.setPointSize(16); f.setBold(True)
        self.tool_title.setFont(f)
        self.tool_title.setAlignment(QtCore.Qt.AlignLeft)

        self.info_btn = QtWidgets.QToolButton()
        self.info_btn.setObjectName("InfoBtn")
        self.info_btn.setToolTip("About this tool")
        self.info_btn.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.info_btn.clicked.connect(self._show_tool_info)

        ICON_PX, PADDING = 30, 2
        SIDE = ICON_PX + PADDING * 2
        self.info_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MessageBoxInformation))
        self.info_btn.setIconSize(QtCore.QSize(ICON_PX, ICON_PX))
        self.info_btn.setFixedSize(SIDE, SIDE)
        self.info_btn.setStyleSheet("""
        QToolButton#InfoBtn { color: white; border: none; }
        QToolButton#InfoBtn:hover   { background: #A7D9FC; }
        QToolButton#InfoBtn:pressed { background: #83C8F7; }
        """)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.tool_title)
        row.addStretch()
        row.addWidget(self.info_btn)
        return row

    # 2) Resizable splitter between Inputs and Logs
    def _create_io_splitter(self, form_box: QtWidgets.QGroupBox, logs_panel: QtWidgets.QWidget) -> QtWidgets.QSplitter:
        """Put inputs (top) and logs (bottom) in a vertical splitter."""
        sp = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        sp.setHandleWidth(6)
        sp.setOpaqueResize(False)
        sp.addWidget(form_box)
        sp.addWidget(logs_panel)
        sp.setStretchFactor(0, 1)
        sp.setStretchFactor(1, 1)
        sp.setSizes([480, 260])  # initial heights
        return sp

    # 3) Main page layout
    def _finalize_layout(self, title_row: QtWidgets.QHBoxLayout, splitter: QtWidgets.QSplitter):
        """Assemble the page: title → splitter → buttons → status."""
        v = self._root_v
        v.addLayout(title_row)
        v.addWidget(splitter, 1)
        v.addLayout(self._btn_row)
        v.addWidget(self.status)

        # keep these (no stretches for child widgets inside splitter)
        v.setStretch(v.indexOf(self._btn_row), 0)
        v.setStretch(v.indexOf(self.status),   0)

    # 4) Button event-trigger
    def _wire_actions(self):
        """Connect button actions to handlers and I/O helpers."""
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn.clicked.connect(self._on_stop)
        self.export_btn.clicked.connect(self._export_params)
        self.import_btn.clicked.connect(self._import_params)
        
    def _safe_clear_form_layout(self):
        lay = self.form_layout
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
    
    def _show_tool_info(self):
        if not self.tool:
            QtWidgets.QMessageBox.information(self, "About this tool", "No tool selected.")
            return

        esc = html.escape
        t    = self.tool
        title = esc(t.name or "Untitled tool")
        notes = esc((t.notes or "—").strip())

        # Build parameters list as HTML
        items = []
        iCountParameter = 0
        for spec in t.inputs:
            
            label = esc(spec.label or spec.name)
            typ   = esc(spec.type or "string")
            req   = "required" if spec.required else "optional"
            iCountParameter += 1

            bits = [f"<b>{iCountParameter} : {label}</b> <span style='color:#888;'>[{typ}, {req}]</span>"]
            if spec.default not in (None, ""):
                shown_default = "••••" if (spec.type or "").lower() == "password" else str(spec.default)
                bits.append(f"<div style='margin-left:.1em'><i>Default:</i> {esc(shown_default)}</div>")
                # bits.append(f"<div style='margin-left:.1em'><i>Default:</i> {esc(str(spec.default))}</div>")
            if spec.choices:
                choices = ", ".join(esc(str(c)) for c in spec.choices)
                bits.append(f"<div style='margin-left:.1em'><i>Choices:</i> {choices}</div>")
            
            items.append("<li>" + "".join(bits) + "</li>")            

        params_html = "<ul style='margin:0 0 0 .1em; padding:0'>" + "".join(items) + "</ul>" if items else "—"

        html_body = f"""
        <div style="font-size: 12pt; font-weight:700; margin-bottom:6px;">{title}</div>
        <div style="margin:10px 0 4px 0;"><b>Tool information</b></div>
        <div>{notes}</div>        
        <div style="margin:8px 0;"><b>Parameters</b></div>
        {params_html}
        """

        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("About this tool")
        msg.setIcon(QtWidgets.QMessageBox.Information)
        msg.setTextFormat(QtCore.Qt.RichText)  # ensure HTML is used
        msg.setText(html_body)
        # optional: allow selecting/copying text
        msg.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse | QtCore.Qt.LinksAccessibleByMouse)
        msg.exec()
    
    def set_tool(self, tool: ToolSpec):
        if self._busy:
            return
        self._busy = True
        self.setUpdatesEnabled(False)
        try:
            self.tool = tool
            self.tool_title.setText(f"<b>{tool.name}</b>")

            # SAFE clear
            self._safe_clear_form_layout()
            self.fields.clear()

            # ===== build dynamic fields of selected tool, mini app =====
            
            iCountParam = 0
            for spec in tool.inputs:
                label = spec.label or spec.name
                
                w: QtWidgets.QWidget
                if spec.type == "string":
                    w = QtWidgets.QLineEdit()
                    if spec.default is not None: w.setText(str(spec.default))
                elif spec.type == "int":
                    w = QtWidgets.QSpinBox(); w.setRange(-10**9, 10**9)
                    if isinstance(spec.default, int): w.setValue(spec.default)
                elif spec.type == "float":
                    w = QtWidgets.QDoubleSpinBox(); w.setRange(-1e12, 1e12); w.setDecimals(6)
                    if isinstance(spec.default, (int,float)): w.setValue(float(spec.default))
                elif spec.type == "date":
                    w = QtWidgets.QDateEdit(); w.setCalendarPopup(True); w.setDisplayFormat("yyyy-MM-dd")
                    if spec.default:
                        qd = QtCore.QDate.fromString(str(spec.default), "yyyy-MM-dd")
                        w.setDate(qd if qd.isValid() else QtCore.QDate.currentDate())
                    else:
                        w.setDate(QtCore.QDate.currentDate())
                elif spec.type == "toggle":
                    w = QtWidgets.QCheckBox()
                    d = str(spec.default).strip().lower() if spec.default is not None else ""
                    w.setChecked(d in ("yes", "true", "on", "1"))
                    
                elif spec.type == "file":
                    line = QtWidgets.QLineEdit()
                    btn  = QtWidgets.QPushButton("...")
                    cnt  = QtWidgets.QWidget()
                    h = QtWidgets.QHBoxLayout(cnt); h.setContentsMargins(0,0,0,0)
                    h.addWidget(line, 1); h.addWidget(btn)
                    def pick_file(checked=False, le=line):
                        fn, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select File", le.text() or self.cwd, "All files (*.*)")
                        if fn: le.setText(fn)
                    btn.clicked.connect(pick_file)
                    w = cnt; w._file_line = line
                elif spec.type == "folder":
                    line = QtWidgets.QLineEdit()
                    btn  = QtWidgets.QPushButton("...")
                    cnt  = QtWidgets.QWidget()
                    h = QtWidgets.QHBoxLayout(cnt); h.setContentsMargins(0,0,0,0)
                    h.addWidget(line, 1); h.addWidget(btn)
                    def pick_folder(checked=False, le=line):
                        fn = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Folder", le.text() or self.cwd)
                        if fn: le.setText(fn)
                    btn.clicked.connect(pick_folder)
                    w = cnt; w._file_line = line
                elif spec.type == "enum":
                    w = QtWidgets.QComboBox()
                    if spec.choices: w.addItems(spec.choices)
                    if spec.default is not None:
                        idx = w.findText(str(spec.default))
                        if idx >= 0: w.setCurrentIndex(idx)
                elif spec.type == "multienum":
                    w = CheckableComboBox()
                    w.setChoices(spec.choices or [])
                    defaults = []
                    if spec.default:
                        if isinstance(spec.default, str):
                            defaults = [s.strip() for s in spec.default.split(",") if s.strip()]
                        elif isinstance(spec.default, (list, tuple)):
                            defaults = list(spec.default)
                    w.setCheckedItems(defaults)
                elif spec.type == "password":
                    le = QtWidgets.QLineEdit()
                    le.setEchoMode(QtWidgets.QLineEdit.Password)

                    # set default if provided (use with care)
                    if spec.default is not None:
                        le.setText(str(spec.default))

                    # add an inline eye icon to toggle visibility
                    act = QtGui.QAction(self)
                    act.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogYesButton))  # simple icon; swap if you have an eye icon
                    act.setCheckable(True)
                    def _toggle_pwd():
                        le.setEchoMode(QtWidgets.QLineEdit.Normal if act.isChecked() else QtWidgets.QLineEdit.Password)
                    act.toggled.connect(_toggle_pwd)

                    le.addAction(act, QtWidgets.QLineEdit.TrailingPosition)
                    w = le

                elif spec.type == "list":
                    w = QtWidgets.QPlainTextEdit()
                    w.setPlaceholderText("-")
                    w.setFixedHeight(100)
                    w.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                    if spec.default:
                        if isinstance(spec.default, str):
                            w.setPlainText(spec.default)
                        elif isinstance(spec.default, (list, tuple)):
                            w.setPlainText("\n".join(map(str, spec.default)))
                else:
                    w = QtWidgets.QLineEdit()

                if hasattr(w, "_file_line") and spec.default:
                    w._file_line.setText(str(spec.default))
                elif isinstance(w, QtWidgets.QLineEdit) and spec.default is not None:
                    w.setText(str(spec.default))

                iCountParam += 1
                
                self.form_layout.addRow(f'{iCountParam} : {label}' + ("" if not spec.required else " *"), w)
                self.fields[spec.name] = w

                if getattr(spec, "readonly", False):
                    if isinstance(w, (QtWidgets.QLineEdit, QtWidgets.QPlainTextEdit)):
                        w.setReadOnly(True)
                    elif isinstance(w, QtWidgets.QComboBox):
                        w.setEnabled(False)

            self.output_dir.setText("")
            self.log.clear()
            self.status.setText("Ready")
        except Exception as e:
            import traceback
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            QtWidgets.QMessageBox.critical(self, "Form Build Error", tb)
        finally:
            # at the very end of set_tool(...)
            QtCore.QTimer.singleShot(0, self._fit_inputs_height)

            self.setUpdatesEnabled(True)
            self._busy = False
            self.update()


    def collect_values(self) -> Dict[str, Any]:
        vals = {}
        if not self.tool:
            return vals
        for spec in self.tool.inputs:
            w = self.fields.get(spec.name)
            if not w: continue
            if spec.type in ("string","file","folder","password"):
                if hasattr(w, "_file_line"):
                    vals[spec.name] = w._file_line.text().strip()
                else:
                    vals[spec.name] = w.text().strip()
            elif spec.type == "int":
                vals[spec.name] = int(w.value())
            elif spec.type == "float":
                vals[spec.name] = float(w.value())
            elif spec.type == "enum":
                vals[spec.name] = w.currentText()
            elif spec.type == "multienum":
                vals[spec.name] = ",".join(self.fields[spec.name].checkedItems())
            elif spec.type == "date":
                vals[spec.name] = w.date().toString("yyyy-MM-dd")
            elif spec.type == "toggle":
                vals[spec.name] = w.isChecked()
            elif spec.type == "list":
                lines = [ln.strip() for ln in w.toPlainText().splitlines() if ln.strip()]
                vals[spec.name] = ",".join(lines)   # e.g. "alpha,beta,gamma"

            else:
                vals[spec.name] = str(w.text()).strip()
        vals["_output_dir"] = self.output_dir.text().strip() or None
        return vals

    def _on_run(self):
        if not self.tool:
            return
        vals = self.collect_values()
        for spec in self.tool.inputs:
            if spec.required:
                v = vals.get(spec.name, "")
                if v in (None, "", []):
                    QtWidgets.QMessageBox.warning(self, "Missing Input", f"'{spec.label or spec.name}' is required.")
                    return
        try:
            cmd = self._build_command(self.tool, vals)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Build Error", str(e))
            return

        self.status.setText("Running...")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        
        self._pending_cmd = cmd
        QtCore.QTimer.singleShot(0, self._start_run)

    def _start_run(self):
        cmd = getattr(self, "_pending_cmd", None)
        if not cmd:
            return

        # Do NOT force cwd changes — inherit current app CWD
        # If you want to pass env, do env = os.environ.copy()
        self.runner = QProcRunner(self)
        self.runner.lineReady.connect(lambda s: self.log.appendPlainText(s))
        self.runner.finished.connect(self._on_finished)
        
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"     # unbuffer stdout/stderr
        env["PYTHONIOENCODING"] = "utf-8" # good for non-ASCII output
        
        # self.runner.start(cmd, cwd=None)   # keep current working directory
        self.runner.start(cmd, cwd=None, env=env) 

    def _on_stop(self):
        if self.runner:
            self.runner.kill()
        self.status.setText("Stopping...")

    def _on_finished(self, code: int):
        self.status.setText(f"Finished with code {code}")
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.runner = None


    def _build_command(self, tool: ToolSpec, vals: Dict[str, Any]) -> List[str]:
        
        
        
        py = sys.executable.replace(chr(92), chr(47))
        
        if not os.path.isfile(py):
            raise ValueError("Python service not found: " + py)

        toggle_names = {i.name for i in tool.inputs if i.type == "toggle"}
        # Provide a common placeholder dictionary
        placeholders = {
            **vals, 
            "python": py, 
            "python_u": f"{py} -u", 
            "script": tool.script or "",
            "output_dir": (vals.get("_output_dir") or "")}

        # Extra toggle-friendly placeholders
        for k in toggle_names:
            on = bool(vals.get(k))
            placeholders[f"{k}_flag"] = f"--{k}" if on else f"--no-{k}"
            placeholders[f"{k}_yn"]   = "yes" if on else "no"
            placeholders[f"{k}_01"]   = "1" if on else "0"
        
        if tool.runner_mode == "module":
            # python -m pkg.module --func function_name --k v ...
            mod, func = parse_module_runner(tool.runner)
            # cmd = [py, "-m", mod, "--func", func]
            cmd = [py, "-u", "-m", mod, "--func", func]

            # add inputs as --key value
            for k, v in vals.items():
                if k == "_output_dir": continue
                cmd += [f"--{k}", str(v)]
            if vals.get("_output_dir"):
                cmd += ["--output_dir", vals["_output_dir"]]
            return cmd

        elif tool.runner_mode == "command":
            # command template, e.g.: "{python} {script} --in {images} --mode {mode} --out {output_dir}"
            if not tool.runner:
                raise ValueError("Command template is empty.")
            # string substitute
            try:
                templ = tool.runner.format(**placeholders)
            except KeyError as e:
                raise ValueError(f"Missing placeholder in template: {e}")
            # shlex split to list
            return shlex.split(templ)
        else:
            raise ValueError("Unknown runner_mode: " + tool.runner_mode)

    def _params_dict(self) -> dict:
        """Build a portable payload of the current tool's parameters."""
        vals = self.collect_values()                 # you already have this
        # strip special/internal keys
        vals = {k: v for k, v in vals.items() if not k.startswith("_")}
        meta = {
            "tool": (self.tool.name if self.tool else None),
            "runner_mode": (self.tool.runner_mode if self.tool else None),
            "runner": (self.tool.runner if self.tool else None),
            "script": (self.tool.script if self.tool else None),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "app": "PLAR",
            "version": 1,
        }
        return {"meta": meta, "values": vals}

    def _apply_params(self, values: dict):
        """Apply a dict of name->value to the current form fields by type."""
        if not self.tool:
            return
        for spec in self.tool.inputs:
            name = spec.name
            if name not in values:
                continue
            v = values[name]
            w = self.fields.get(name)
            if not w:
                continue

            t = spec.type or "string"
            try:
                if t in ("string", "file", "folder", "password"):
                    if hasattr(w, "_file_line"):
                        w._file_line.setText(str(v or ""))
                    elif isinstance(w, QtWidgets.QLineEdit):
                        w.setText(str(v or ""))
                elif t == "int" and isinstance(w, QtWidgets.QSpinBox):
                    w.setValue(int(v))
                elif t == "float" and isinstance(w, QtWidgets.QDoubleSpinBox):
                    w.setValue(float(v))
                elif t == "enum" and isinstance(w, QtWidgets.QComboBox):
                    idx = w.findText(str(v))
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                elif t == "multienum" and hasattr(w, "setCheckedItems"):
                    if isinstance(v, str):
                        items = [s.strip() for s in v.split(",") if s.strip()]
                    elif isinstance(v, (list, tuple)):
                        items = list(v)
                    else:
                        items = []
                    w.setCheckedItems(items)
                elif t == "date" and isinstance(w, QtWidgets.QDateEdit):
                    qd = QtCore.QDate.fromString(str(v), "yyyy-MM-dd")
                    if qd.isValid():
                        w.setDate(qd)
                elif t == "toggle" and isinstance(w, QtWidgets.QCheckBox):
                    # accept bool, "yes/no", "true/false", "1/0"
                    sv = str(v).strip().lower()
                    on = v is True or sv in ("1", "true", "yes", "on")
                    w.setChecked(on)
                elif t == "list" and isinstance(w, QtWidgets.QPlainTextEdit):
                    # accept CSV or list; store each on its own line
                    if isinstance(v, str):
                        items = [s.strip() for s in v.split(",") if s.strip()]
                    elif isinstance(v, (list, tuple)):
                        items = list(v)
                    else:
                        items = []
                    w.setPlainText("\n".join(items))
                else:
                    # default / fallback
                    if isinstance(w, QtWidgets.QLineEdit):
                        w.setText(str(v or ""))
            except Exception as e:
                # Non-fatal: continue applying what we can
                self.log.appendPlainText(f"[apply] {name}: {e}")
        QtCore.QTimer.singleShot(0, self._fit_inputs_height)

    def _export_params(self):
        """Export current parameters to a JSON file."""
        if not self.tool:
            QtWidgets.QMessageBox.information(self, "Export", "No tool selected.")
            return
        safe_tool = "".join(c for c in (self.tool.name or "tool") if c.isalnum() or c in (" ","-","_")).strip()
        suggested = f"{safe_tool} - {time.strftime('%Y%m%d-%H%M%S')}.plar.json"
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export Parameters", suggested, "PLAR Settings (*.plar.json);;JSON (*.json);;All files (*.*)"
        )
        if not fn:
            return
        try:
            payload = self._params_dict()
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            self.status.setText(f"Exported parameters → {Path(fn).name}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Export Failed", str(e))

    def _import_params(self):
        """Import parameters from a JSON file and apply to current form."""
        if not self.tool:
            QtWidgets.QMessageBox.information(self, "Import", "No tool selected.")
            return
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import Parameters", "", "PLAR Settings (*.plar.json *.json);;All files (*.*)"
        )
        if not fn:
            return
        try:
            with open(fn, "r", encoding="utf-8") as f:
                data = json.load(f)
            # accept either {"meta":..., "values":...} or plain dict of values
            meta = data.get("meta", {}) if isinstance(data, dict) else {}
            values = data.get("values", data if isinstance(data, dict) else {})

            # Friendly guard: tool mismatch
            meta_tool = (meta.get("tool") or "").strip()
            cur_tool = (self.tool.name or "").strip()
            if meta_tool and meta_tool != cur_tool:
                ans = QtWidgets.QMessageBox.question(
                    self,
                    "Apply anyway?",
                    f"Settings were saved for tool:\n  '{meta_tool}'\n\nCurrent tool is:\n  '{cur_tool}'\n\nApply anyway?"
                )
                if ans != QtWidgets.QMessageBox.Yes:
                    return

            self._apply_params(values)
            self.status.setText(f"Imported parameters ← {Path(fn).name}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Import Failed", str(e))



#-------------- combo box with icons --------------
# ======== class CheckableComboBox ========================================================
# =========================================================================================
class CheckableComboBox(QtWidgets.QComboBox):
    """Multi-select dropdown with checkmarks + one-line summary, aligned popup."""
    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setView(QtWidgets.QListView())
        self.setModel(QtGui.QStandardItemModel(self))

        # Use editable line edit to control the displayed text ourselves
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setPlaceholderText("— select —")
        self.setInsertPolicy(QtWidgets.QComboBox.NoInsert)

        # Show more items when open
        self.setMaxVisibleItems(14)

        # Toggle items on click
        self.view().pressed.connect(self._toggle_item)

        # Keep the summary in sync when model changes in any way
        self.model().dataChanged.connect(self._update_text)
        self.model().rowsInserted.connect(self._update_text)
        self.model().rowsRemoved.connect(self._update_text)
        self.currentIndexChanged.connect(self._update_text)

        # Avoid built-in text overriding our summary
        self.setCurrentIndex(-1)

    # ----- public API
    def setChoices(self, choices):
        self.model().clear()
        for text in (choices or []):
            it = QtGui.QStandardItem(str(text))
            it.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable)
            it.setData(QtCore.Qt.Unchecked, QtCore.Qt.CheckStateRole)
            self.model().appendRow(it)
        self._update_text()

    def setCheckedItems(self, items):
        want = set(map(str, items or []))
        for row in range(self.model().rowCount()):
            it = self.model().item(row)
            it.setCheckState(QtCore.Qt.Checked if it.text() in want else QtCore.Qt.Unchecked)
        self._update_text()

    def checkedItems(self):
        out = []
        for row in range(self.model().rowCount()):
            it = self.model().item(row)
            if it.checkState() == QtCore.Qt.Checked:
                out.append(it.text())
        return out

    # ----- internals
    def _toggle_item(self, idx: QtCore.QModelIndex):
        it = self.model().itemFromIndex(idx)
        it.setCheckState(QtCore.Qt.Unchecked if it.checkState() == QtCore.Qt.Checked
                         else QtCore.Qt.Checked)
        self._update_text()
        self.changed.emit()

    def _update_text(self):
        sel = self.checkedItems()
        full = ", ".join(sel) if sel else "— none —"

        # one-line summary: first 20, then (+N)
        MAX_SHOW = 20
        display = full if len(sel) <= MAX_SHOW else ", ".join(sel[:MAX_SHOW]) + f" (+{len(sel)-MAX_SHOW})"

        # elide to widget width
        fm = self.lineEdit().fontMetrics()
        elided = fm.elidedText(display, QtCore.Qt.ElideRight, max(60, self.width() - 28))
        self.lineEdit().setText(elided)
        self.setToolTip(full)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_text()  # keep eliding correct on resize

    def showPopup(self):
        super().showPopup()
        # Make popup the same width and aligned under the combo (no shift)
        popup = self.view().window()  # QFrame created by QComboBox
        # popup.setFixedWidth(self.width())
        popup.setFixedWidth(int(self.width() * 1.15))
        popup.move(self.mapToGlobal(QtCore.QPoint(0, self.height())))


# ---------- Main Window ----------
# ======== class MainWindow ===============================================================
# =========================================================================================
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, tools: List[ToolSpec]):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(QtGui.QIcon(str(APP_ASSET(APP_ICON_FILE))))
        self.resize(1100, 700)
        self.tools: List[ToolSpec] = tools
        
        self._select_timer = QtCore.QTimer()
        self._select_timer.setSingleShot(True)
        self._select_timer.timeout.connect(self._apply_selection)

        # Left: tools list
        self.list = QtWidgets.QListWidget()
        self.list.itemSelectionChanged.connect(self._on_select)
        self.list.setMinimumWidth(260)
        self.list.setAlternatingRowColors(True)
        self.list.setSpacing(2)
        
        # NEW: tame selection spam & double-clicks
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.list.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.list.installEventFilter(self)

        self._last_applied_row = -1     # remember last row we actually built
        
        self.list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_list_menu)

        font = self.list.font()
        font.setPointSize(11)          # try 13–15
        # font.setBold(True)             # optional
        self.list.setFont(font)
        
        
        self._reload_list()

        # Right: form
        self.form = ToolForm()

        # ===== Menu Bar =====
        mb = self.menuBar()
        
        
        # bump menu fonts
        f = mb.font()
        f.setPointSize(f.pointSize() + 2)   # or: f.setPointSize(12)
        mb.setFont(f)

        mb.setStyleSheet("""
        QMenuBar {
            background: #f3f4f6;
            border-bottom: 1px solid rgba(0,0,0,0.15);
            padding: 4px 8px;
            font-size: 12pt; 
        }
        QMenuBar::item {
            padding: 4px 10px;
            font-weight: 600;
            color: #222;
            background: transparent;
        }
        QMenuBar::item:selected { background: rgba(37, 99, 235, 0.12); border-radius: 6px; }
        QMenu { background: #ffffff; border: 1px solid #ddd; padding: 6px 0; font-size: 12pt; }
        QMenu::item { padding: 6px 14px; }
        QMenu::item:selected { background: rgba(37, 99, 235, 0.12); }
        """)
        
        # ---- Actions (create FIRST)
        add_act   = QtGui.QAction("Add Tool", self);    add_act.triggered.connect(self._add_tool)
        edit_act  = QtGui.QAction("Edit Tool", self);   edit_act.triggered.connect(self._edit_tool)
        dup_act   = QtGui.QAction("Duplicate", self);   dup_act.triggered.connect(self._dup_tool)
        del_act   = QtGui.QAction("Delete", self);      del_act.triggered.connect(self._del_tool)
        save_act  = QtGui.QAction("Save Config", self); save_act.triggered.connect(self._save)
        load_cfg_act = QtGui.QAction("Load Config File…", self)
        
        
        # optional shortcuts
        add_act.setShortcut("Ctrl+N")
        edit_act.setShortcut("Ctrl+E")
        dup_act.setShortcut("Ctrl+D")
        del_act.setShortcut("Del")
        load_cfg_act.setShortcut("Ctrl+L") 

        # # --- Theme menu (stable switching) ---
        # theme_menu = mb.addMenu("Theme")

        # act_theme_light = QtGui.QAction("Light", self, checkable=True)
        # act_theme_dark  = QtGui.QAction("Dark",  self, checkable=True)
        # act_theme_auto  = QtGui.QAction("Auto (follow system)", self, checkable=True)
        # theme_group = QtGui.QActionGroup(self)
        # for a in (act_theme_light, act_theme_dark, act_theme_auto):
        #     a.setActionGroup(theme_group)
        # theme_menu.addActions([act_theme_light, act_theme_dark, act_theme_auto])

        # # load current mode to set the checked state
        # cur_mode = QtCore.QSettings("PlarApp", "LocalAppRunner").value("theme", "auto")
        # (act_theme_light if cur_mode=="light" else act_theme_dark if cur_mode=="dark" else act_theme_auto).setChecked(True)

        # act_theme_light.triggered.connect(lambda: self._switch_theme("light"))
        # act_theme_dark.triggered.connect(lambda: self._switch_theme("dark"))
        # act_theme_auto.triggered.connect(lambda: self._switch_theme("auto"))

        
        load_cfg_act.triggered.connect(self._load_config_file)
        

        # ---- Menus (add actions AFTER they exist)
        m_tools = mb.addMenu("Tools")
        m_tools.addAction(add_act)
        m_tools.addAction(edit_act)
        m_tools.addAction(dup_act)
        m_tools.addSeparator()
        m_tools.addAction(del_act)
        m_tools.addSeparator()
        m_tools.addAction(load_cfg_act)

        m_file = mb.addMenu("File")
        m_file.addAction(save_act)

        # Central splitter
        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.list)
        splitter.addWidget(self.form)
        splitter.setSizes([320, 900])     # initial widths for [left, right]
        splitter.setStretchFactor(0, 0)   # left pane doesn’t auto-grow
        splitter.setStretchFactor(1, 1)   # right pane takes extra space
        
        # In MainWindow.__init__ after creating splitter
        splitter.setHandleWidth(6)
        margins = 12
        central = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(central)
        lay.setContentsMargins(margins, margins, margins, margins)
        lay.addWidget(splitter)
        self.setCentralWidget(central)
        
        # Global (in-window) shortcut: Ctrl+R, Ctrl+Enter, Ctrl+Return to run the selected tool
        for seq in ("Ctrl+R", "Ctrl+Return", "Ctrl+Enter", "F2"):
            sc = QtGui.QShortcut(QtGui.QKeySequence(seq), self)
            sc.activated.connect(self._shortcut_run)
        
        # smoother divider drag (less repaint work)
        splitter.setOpaqueResize(False)

        # coalesced resize guard
        self._resizing = False
        self._resize_coalesce = QtCore.QTimer(self)
        self._resize_coalesce.setSingleShot(True)
        self._resize_coalesce.setInterval(140)  # 120–180ms feels good
        self._resize_coalesce.timeout.connect(self._end_resize)

        self.statusBar().showMessage("Ready")

        if self.list.count() > 0:
            self.list.setCurrentRow(0)

    def resizeEvent(self, event):
        # On first tick of a resize burst, freeze heavy widgets only
        if not self._resizing:
            self._resizing = True
            # freeze only the expensive area, not the whole window
            self.form.setUpdatesEnabled(False)
            self.form.log.setUpdatesEnabled(False)
        # restart the coalescing timer on every tick
        self._resize_coalesce.start()
        super().resizeEvent(event)

    def _end_resize(self):
        # re-enable updates once user pauses
        self.form.log.setUpdatesEnabled(True)
        self.form.setUpdatesEnabled(True)
        self._resizing = False
        # one clean repaint
        self.form.update()
    def eventFilter(self, obj, ev):
        if obj is self.list and ev.type() == QtCore.QEvent.MouseButtonDblClick:
            return True  # swallow double-clicks; we only want single selection changes
        return super().eventFilter(obj, ev)
    
    def _current_tool_index(self) -> int:
        idx = self.list.currentRow()
        return idx if 0 <= idx < len(self.tools) else -1

    def _run_selected(self):
        # Just trigger the form’s run (only if there’s a tool selected)
        if self._current_tool_index() >= 0:
            self.form._on_run()

    def _move_tool(self, delta: int):
        """Move selected tool up/down by delta (+1 / -1)."""
        idx = self._current_tool_index()
        if idx < 0:
            return
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.tools)):
            return
        self.tools[idx], self.tools[new_idx] = self.tools[new_idx], self.tools[idx]
        self._reload_list()
        self.list.setCurrentRow(new_idx)
        self._save(silent=True)

    def _show_list_menu(self, pos: QtCore.QPoint):
        item = self.list.itemAt(pos)
        has_item = item is not None
        global_pos = self.list.mapToGlobal(pos)

        s = self.style()
        menu = QtWidgets.QMenu(self)

        # Top actions (item-specific)
        act_run  = menu.addAction(s.standardIcon(QtWidgets.QStyle.SP_MediaPlay), "Run", self._run_selected)
        act_info = menu.addAction(s.standardIcon(QtWidgets.QStyle.SP_MessageBoxInformation), "Info", 
                                lambda: self.form._show_tool_info() if has_item else None)
        menu.addSeparator()

        act_add  = menu.addAction(s.standardIcon(QtWidgets.QStyle.SP_FileDialogNewFolder), "Add Tool", self._add_tool)
        act_edit = menu.addAction(s.standardIcon(QtWidgets.QStyle.SP_FileDialogDetailedView), "Edit Tool", self._edit_tool)
        act_dup  = menu.addAction(s.standardIcon(QtWidgets.QStyle.SP_DialogOkButton), "Duplicate", self._dup_tool)
        act_del  = menu.addAction(s.standardIcon(QtWidgets.QStyle.SP_TrashIcon), "Delete", self._del_tool)
        menu.addSeparator()

        act_up   = menu.addAction("Move Up",   lambda: self._move_tool(-1))
        act_down = menu.addAction("Move Down", lambda: self._move_tool(+1))
        menu.addSeparator()

        act_save = menu.addAction("Save Config", self._save)

        # Enable/disable depending on whether we clicked an item
        for a in (act_run, act_info, act_edit, act_dup, act_del, act_up, act_down):
            a.setEnabled(has_item)

        # Show the menu
        menu.exec(global_pos)
    
    def _focus_main_and_select(self):
        # Show the main window if minimized / hidden
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

        # Ensure a tool is selected
        if self._current_tool_index() < 0 and self.list.count() > 0:
            row = self._last_applied_row if self._last_applied_row >= 0 else 0
            self.list.setCurrentRow(max(0, min(row, self.list.count() - 1)))
            QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.AllEvents, 50)

    def _shortcut_run(self):
        self._focus_main_and_select()
        self._run_selected()

    
    def _reload_list(self):
    # keep current selection if you want
        current_name = None
        if (it := self.list.currentItem()):
            current_name = it.data(QtCore.Qt.UserRole) or it.text()

        self.list.blockSignals(True)
        self.list.clear()

        digits = max(2, len(str(len(self.tools))))  # or just use 2

        for i, t in enumerate(self.tools):
            label = f"{i+1:0{digits}d} : {t.name}"

            it = QtWidgets.QListWidgetItem(label)  # <-- NEW item each iteration
            it.setData(QtCore.Qt.UserRole, t.name)
            self.list.addItem(it)

        # restore selection by name (optional)
        if current_name:
            for row in range(self.list.count()):
                if self.list.item(row).data(QtCore.Qt.UserRole) == current_name:
                    self.list.setCurrentRow(row)
                    break

        self.list.blockSignals(False)


    def _on_select(self):
        self._select_timer.start(150)

    def _apply_selection(self):
        idx = self.list.currentRow()
        if idx < 0 or idx >= len(self.tools):
            self.form.set_tool(ToolSpec(name=""))
            self._last_applied_row = -1
            return

        # Ignore redundant selection (prevents unnecessary rebuilds)
        if idx == self._last_applied_row:
            return

        self.list.setEnabled(False)
        try:
            tool = self.tools[idx]
            # Queue the heavy rebuild to the next event-loop turn (avoids re-entrancy)
            QtCore.QTimer.singleShot(0, lambda t=tool: self.form.set_tool(t))
            self._last_applied_row = idx

            # Bold highlight for the selected one
            for i in range(self.list.count()):
                it = self.list.item(i)
                f = it.font()
                f.setBold(i == idx)
                it.setFont(f)
        finally:
            # Re-enable slightly later so pending paints finish first
            QtCore.QTimer.singleShot(120, lambda: self.list.setEnabled(True))

    
    def _add_tool(self):
        dlg = ToolEditor(self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            t = dlg.result_tool()
            self.tools.append(t)
            self._reload_list()
            self._save(silent=True)

    def _edit_tool(self):
        items = self.list.selectedItems()
        if not items: return
        idx = self.list.currentRow()
        cur = self.tools[idx]
        
        dlg = ToolEditor(self, cur)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.tools[idx] = dlg.result_tool()
            self._reload_list()
            self._last_applied_row = -1            # <-- allow rebuild on same row
            self.list.setCurrentRow(idx)
            # also refresh the right pane immediately so the new 'required' flags apply
            QtCore.QTimer.singleShot(0, lambda: self.form.set_tool(self.tools[idx]))
            self._save(silent=True)

    def _dup_tool(self):
        items = self.list.selectedItems()
        if not items: return
        idx = self.list.currentRow()
        base = self.tools[idx]
        clone = ToolSpec(
            name=base.name + " (copy)",
            runner_mode=base.runner_mode,
            runner=base.runner,
            script=base.script,
            # output_dir_optional=base.output_dir_optional,
            output_dir_optional=False,
            inputs=[InputSpec(**i.__dict__) for i in base.inputs],
            notes=base.notes
        )
        self.tools.append(clone)
        self._reload_list()
        self.list.setCurrentRow(self.list.count()-1)
        self._save(silent=True)

    def _del_tool(self):
        items = self.list.selectedItems()
        if not items: return
        idx = self.list.currentRow()
        name = self.tools[idx].name
        if QtWidgets.QMessageBox.question(self, "Delete", f"Delete tool '{name}'?") == QtWidgets.QMessageBox.Yes:
            del self.tools[idx]
            self._reload_list()
            self._save(silent=True)

    def _save(self, silent=False):
        save_config(CONFIG_FILE, self.tools)
        if not silent:
            QtWidgets.QMessageBox.information(self, "Saved", f"Saved to {CONFIG_FILE}")
    
    def _load_config_file(self):
        """Pick a JSON file and overwrite CONFIG_FILE, then reload list/UI."""
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select Tools Config (JSON)", "", "JSON files (*.json);;All files (*.*)"
        )
        if not fn:
            return

        try:
            # Validate JSON first so we don't clobber current file with bad data
            with open(fn, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("Config must be a JSON array of tools.")

            # Overwrite the active config file directly
            with open(CONFIG_FILE, "w", encoding="utf-8") as out:
                json.dump(data, out, indent=2, ensure_ascii=False)

            # Reload into the app
            self.tools = load_config(CONFIG_FILE)
            self._reload_list()
            if self.list.count() > 0:
                self.list.setCurrentRow(0)

            QtWidgets.QMessageBox.information(
                self, "Config Loaded",
                f"Replaced and reloaded config from:\n{fn}"
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "Load Failed",
                f"Could not load config:\n{e}"
            )

    def _switch_theme(self, mode: str):
        app = QtWidgets.QApplication.instance()
        if not app:
            return

        apply_modern_theme(app, mode)
        app.setStyle("Fusion")  # stable base

        style = app.style()
        for w in app.allWidgets():
            style.unpolish(w)
            style.polish(w)
            # ---- SAFE refresh that avoids QListWidget.update(index) overload
            if isinstance(w, QtWidgets.QAbstractItemView):
                # views repaint their viewport
                w.viewport().update()
            else:
                try:
                    QtWidgets.QWidget.update(w)  # call QWidget::update explicitly
                except TypeError:
                    w.repaint()  # final fallback

        dark = is_dark_palette(app.palette())   # helper you already have
        for tb in self.findChildren(QtWidgets.QToolBar):
            style_toolbar(tb, dark)             # uses your dark/light CSS
        
        QtCore.QSettings("PlarApp", "LocalAppRunner")
        if self.statusBar():
            self.statusBar().showMessage(f"Theme: {mode}", 2000)


# --- Modern theme (light/dark) helpers ---
def apply_modern_theme(app: QtWidgets.QApplication, mode: str = "light"):
    """
    mode: 'light' | 'dark' | 'auto'
    """
    if mode == "auto":
        # follow Windows setting if available; fallback to light
        mode = "dark" if QtGui.QGuiApplication.palette().color(QtGui.QPalette.Window).value() < 128 else "light"

    app.setStyle("Fusion")  # stable + themeable

    # Typography
    base = app.font()
    base.setFamily("Segoe UI")
    base.setPointSize(11)       # comfortable default
    app.setFont(base)

    pal = QtGui.QPalette()

    if mode == "dark":
        # Windows 11-ish dark palette
        bg   = QtGui.QColor(32, 32, 36)
        card = QtGui.QColor(42, 42, 48)
        txt  = QtGui.QColor(230, 230, 235)
        sub  = QtGui.QColor(180, 182, 188)
        acc  = QtGui.QColor("#ADE4F7")  # primary accent (Win11 blue-ish)

        pal.setColor(QtGui.QPalette.Window, bg)
        pal.setColor(QtGui.QPalette.Base, card)
        pal.setColor(QtGui.QPalette.AlternateBase, bg.darker(110))
        pal.setColor(QtGui.QPalette.ToolTipBase, card)
        pal.setColor(QtGui.QPalette.ToolTipText, txt)
        pal.setColor(QtGui.QPalette.Text, txt)
        pal.setColor(QtGui.QPalette.Button, card)
        pal.setColor(QtGui.QPalette.ButtonText, txt)
        pal.setColor(QtGui.QPalette.Highlight, acc)
        pal.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
        pal.setColor(QtGui.QPalette.PlaceholderText, sub)
        pal.setColor(QtGui.QPalette.WindowText, txt)
    else:
        # Light palette
        bg   = QtGui.QColor(246, 246, 248)
        card = QtGui.QColor(255, 255, 255)
        txt  = QtGui.QColor(24, 24, 28)
        sub  = QtGui.QColor(110, 113, 120)
        acc  = QtGui.QColor("#ADE4F7")

        pal.setColor(QtGui.QPalette.Window, bg)
        pal.setColor(QtGui.QPalette.Base, QtGui.QColor(250, 250, 251))
        pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(244, 244, 246))
        pal.setColor(QtGui.QPalette.ToolTipBase, card)
        pal.setColor(QtGui.QPalette.ToolTipText, txt)
        pal.setColor(QtGui.QPalette.Text, txt)
        pal.setColor(QtGui.QPalette.Button, card)
        pal.setColor(QtGui.QPalette.ButtonText, txt)
        pal.setColor(QtGui.QPalette.Highlight, acc)
        pal.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
        pal.setColor(QtGui.QPalette.PlaceholderText, sub)
        pal.setColor(QtGui.QPalette.WindowText, txt)
        
        

    app.setPalette(pal)

    # Global stylesheet (rounded corners, card group boxes, nicer fields & buttons)
    app.setStyleSheet("""
        /* Headings */
        QLabel#Heading {
            font-size: 18px;
            font-weight: 700;
            padding: 4px 0 10px 0;
        }

        /* Left tool list */
        QListWidget {
            border: none;
            padding: 8px;
            outline: none;
            background-color: #80BEE8F7 ;        /* #80BEE8F7  transparent with slight tint blue */
        }
        QListWidget::item {
            padding: 8px 10px;
            border-radius: 8px;
            margin: 2px 0;
            background-color: #33BEE8F7;        /*  #33BEE8F7  transparent with slight tint blue */
        }
        QListWidget::item:selected {
            background: #ADE4F7;
            color: black;
        }
        QListView {
            padding: 4px 6px;
        }
        QListView::item {
            padding: 4px 6px;
        }

        /* Card group */
        QGroupBox {
            background: palette(Base);
            border: 1px solid rgba(0,0,0,25%);
            border-radius: 12px;
            margin-top: 18px;
            padding: 12px 12px 8px 12px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            top: 6px;
            padding: 0 6px;
            background: transparent;
            font-weight: 600;
        }

        /* Form spacing */
        QFormLayout > * {
            margin-top: 6px;
            margin-bottom: 6px;
        }
        QLabel {
            color: palette(WindowText);
        }

        /* Inputs */
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
            border: 1px solid rgba(0,0,0,25%);
            border-radius: 8px;
            padding: 6px 8px;
            background: palette(Base);
        }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {
            border: 2px solid palette(Highlight);
        }

        /* Buttons */
        QPushButton {
            border: 1px solid rgba(0,0,0,16%);
            border-radius: 10px;
            padding: 8px 14px;
            font-weight: 600;
            background: palette(Button);
        }
        QPushButton:hover {
            border-color: rgba(0,0,0,28%);
        }
        QPushButton#Primary {
            background: palette(Highlight);
            color: white;
            border: none;
        }
        QPushButton#Danger {
            background: #ed4c4c;
            color: white;
            border: none;
        }

        /* Logs */
        QPlainTextEdit#LogView {
            border-radius: 10px;
            border: 1px solid rgba(0,0,0,20%);
            padding: 8px;
            background: palette(Base);
        }

        /* Toolbar */
        QToolBar {
            border: none;
            padding: 6px;
            spacing: 8px;
        }
        
        QGroupBox {
            border: none;
            background: transparent;
        }
        QFormLayout QLabel {
            font-size: 13px;
            color: #555;
            padding-bottom: 2px;
        }
        QLineEdit, QComboBox, QTextEdit {
            border: none;
            border-bottom: 2px solid rgba(0,0,0,0.15);
            border-radius: 0;
            padding: 4px;
            background: transparent;
        }
        QLineEdit:focus, QComboBox:focus {
            border-bottom: 2px solid #1c2121; 
        }
        
        QGroupBox QLineEdit,
        QGroupBox QComboBox,
        QGroupBox QSpinBox,
        QGroupBox QDoubleSpinBox,
        QGroupBox QPlainTextEdit,
        QGroupBox QDateEdit {
            background: #ffffff;
            border: 1px solid rgba(0,0,0,0.25);
            border-radius: 8px;
            padding: 6px 8px;
        }

        /* Softer look for read-only / disabled */
        QGroupBox QLineEdit[readOnly="true"],
        QGroupBox QPlainTextEdit[readOnly="true"],
        QGroupBox QComboBox:disabled,
        QGroupBox QSpinBox:disabled,
        QGroupBox QDoubleSpinBox:disabled,
        QGroupBox QDateEdit:disabled {
            background: #f3f3f3;
        }
        
        
        /* --- Force BLACK text for controls inside the Inputs group --- */
        QGroupBox QLineEdit,
        QGroupBox QPlainTextEdit,
        QGroupBox QSpinBox,
        QGroupBox QDoubleSpinBox,
        QGroupBox QDateEdit,
        QGroupBox QComboBox {
            color: #000000;
        }

        /* ComboBox popup list items */
        QGroupBox QComboBox QAbstractItemView {
            color: #000000;
            background: #ffffff;
        }

        /* Editable/summary text inside (e.g., your CheckableComboBox uses a line edit) */
        QGroupBox QComboBox QLineEdit {
            color: #000000;
            background: transparent;   /* keep your white field from parent */
        }

        /* Keep read-only fields black too */
        QGroupBox QLineEdit[readOnly="true"],
        QGroupBox QPlainTextEdit[readOnly="true"] {
            color: #000000;
        }

        
    """)

    
    popup_css = """
        /* ===== Popups (unified light blue look) ===== */
        QMessageBox, QInputDialog, QColorDialog, QFontDialog, QFileDialog {
            background: #ffffff;
        }

        QMessageBox QLabel, QInputDialog QLabel, QFileDialog QLabel {
            color: #1f2937;
            font-size: 12pt;
        }

        QMessageBox QPushButton, QInputDialog QPushButton, QFileDialog QPushButton, 
        QColorDialog QPushButton, QFontDialog QPushButton {
            background: #ADE4F7;
            color: black;
            border: none;
            border-radius: 8px;
            padding: 6px 12px;
            font-weight: 600;
        }
        QMessageBox QPushButton:hover, QInputDialog QPushButton:hover, QFileDialog QPushButton:hover,
        QColorDialog QPushButton:hover, QFontDialog QPushButton:hover {
            background: #93D8F3;
        }

        /* File dialog lists/edits */
        QFileDialog QListView, QFileDialog QTreeView, QFileDialog QLineEdit {
            background: #ffffff;
            color: #1f2937;
            border: 1px solid rgba(0,0,0,0.2);
            border-radius: 6px;
        }

        /* Tooltips: soft light-blue */
        QToolTip {
            background: #E6F6FE;
            color: #111827;
            border: 1px solid #BEE8F7;
            padding: 6px 8px;
            border-radius: 6px;
        }

        /* Menus inside dialogs */
        QMenu {
            background: #ffffff;
            border: 1px solid rgba(0,0,0,0.15);
        }
        QMenu::item {
            padding: 6px 12px;
            border-radius: 6px;
        }
        QMenu::item:selected {
            background: #E6F6FE;
            color: #111827;
        }
        """
        # Append to the existing global stylesheet (don’t overwrite it)
    app.setStyleSheet(app.styleSheet() + popup_css)

def is_dark_palette(pal: QtGui.QPalette) -> bool:
    bg = pal.color(QtGui.QPalette.Window)
    # luminance check
    return (0.2126*bg.redF() + 0.7152*bg.greenF() + 0.0722*bg.blueF()) < 0.5

def set_readable_selection(app: QtWidgets.QApplication):
    pal = app.palette()
    if is_dark_palette(pal):
        pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("white"))
        pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#3a63b8"))
    else:
        pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("black"))
        pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#cde2ff"))
    app.setPalette(pal)

def style_toolbar(tb: QtWidgets.QToolBar, dark: bool):
    if not tb:
        return
    if dark:
        tb.setStyleSheet("""
            QToolBar { background:#202124; border-bottom:1px solid rgba(255,255,255,0.1); padding:4px 8px; spacing:6px; }
            QToolButton { background:transparent; border:none; padding:4px 10px; font-weight:600; color:#eaeaea; }
            QToolButton:hover { background:rgba(255,255,255,0.08); border-radius:6px; }
            QToolButton:pressed { background:rgba(255,255,255,0.18); border-radius:6px; }
        """)
    else:
        tb.setStyleSheet("""
            QToolBar { background:#f3f4f6; border-bottom:1px solid rgba(0,0,0,0.15); padding:4px 8px; spacing:6px; }
            QToolButton { background:transparent; border:none; padding:4px 10px; font-weight:600; color:#222; }
            QToolButton:hover { background:rgba(37,99,235,0.12); border-radius:6px; }
            QToolButton:pressed { background:rgba(37,99,235,0.25); color:white; border-radius:6px; }
        """)


def normalize_font_weights(widget: QtWidgets.QWidget):
    """Clamp all descendant widget font weights to the valid 1..1000 range."""
    for w in widget.findChildren(QtWidgets.QWidget):
        f: QtGui.QFont = w.font()
        wt = f.weight()
        if wt < 1:
            f.setWeight(1)
            w.setFont(f)
        elif wt > 1000:
            # pick a sane, readable weight
            f.setWeight(QtGui.QFont.Weight.DemiBold)  # 600
            w.setFont(f)

def _qt_msg_filter(mode, ctx, msg):
    # Drop the noisy font-weight warning (harmless)
    if 'QFont::setWeight' in msg:
        return
    # Forward everything else
    sys.stderr.write(msg + '\n')

QtCore.qInstallMessageHandler(_qt_msg_filter)

def main():
    os.chdir(APP_DIR())
    app = QtWidgets.QApplication(sys.argv)
    app.setWindowIcon(QtGui.QIcon(str(APP_ASSET(APP_ICON_FILE))))
    # apply_modern_theme(app, mode="auto")  # Light/Dark toggle also available in toolbar
    # settings = QtCore.QSettings("PlarApp", "LocalAppRunner")
    # start_mode = settings.value("theme", "auto")
    # apply_modern_theme(app, mode=start_mode)
    apply_modern_theme(app, mode='light')

    tools = load_config(CONFIG_FILE)
    
    def _show_crash_box(exctype, value, tb):
        import traceback
        msg = "".join(traceback.format_exception(exctype, value, tb))
        QtWidgets.QMessageBox.critical(None, "Unhandled Error", msg)

    # ...
    w = MainWindow(tools)
    w.show()
    sys.excepthook = _show_crash_box   # <-- add this line
       
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

# 