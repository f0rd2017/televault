from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from app.ui.dialogs._style import _DIALOG_STYLESHEET


class SetupDialog(QDialog):
    """Minimal settings dialog — shows only fields users actually configure."""

    def __init__(self, initial: dict[str, Any] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Initial Setup"))
        initial = initial or {}

        # ── Preserve hidden fields (passed through unchanged on save) ──────
        self._caption_prefix = (
            str(initial.get("caption_prefix", "FC1|")).strip() or "FC1|"
        )
        self._scan_search = (
            str(initial.get("scan_search", self._caption_prefix)).strip()
            or self._caption_prefix
        )
        self._use_sha_as_key = bool(initial.get("use_sha_as_key", True))
        self._chunk_size_mb = int(initial.get("chunk_size_mb", 512))
        self._download_integrity_mode = (
            str(initial.get("download_integrity_mode", "fast")).strip().lower()
        )
        if self._download_integrity_mode not in {"strict", "fast"}:
            self._download_integrity_mode = "fast"
        self._upload_compression_mode = (
            str(initial.get("upload_compression_mode", "auto")).strip().lower()
        )
        self._upload_limit_safety_mb = int(initial.get("upload_limit_safety_mb", 100))
        self._balanced_part_min_file_mb = int(
            initial.get("balanced_part_min_file_mb", 512)
        )
        self._small_file_threshold_kb = int(
            initial.get("small_file_threshold_kb", 8192)
        )
        self._small_file_batch_target_mb = int(
            initial.get("small_file_batch_target_mb", 48)
        )
        self._small_upload_parallel_jobs = int(
            initial.get("small_upload_parallel_jobs", 3)
        )
        self._small_batch_mode = str(initial.get("small_batch_mode", "global"))
        self._small_batch_max_files = int(initial.get("small_batch_max_files", 512))
        self._small_batch_manifest_mode = str(
            initial.get("small_batch_manifest_mode", "inline_local")
        )
        self._send_media_rate_limit = float(initial.get("send_media_rate_limit", 6.0))
        self._get_file_rate_limit = float(initial.get("get_file_rate_limit", 16.0))
        self._lane_upload_small_max = int(initial.get("lane_upload_small_max", 2))
        self._lane_upload_large_max = int(initial.get("lane_upload_large_max", 2))
        self._lane_download_max = int(initial.get("lane_download_max", 3))
        self._perf_telemetry_window_sec = float(
            initial.get("perf_telemetry_window_sec", 1.0)
        )
        self._cache_max_size_mb = int(initial.get("cache_max_size_mb", 0))
        self._stream_cache_max_mb = int(initial.get("stream_cache_max_mb", 2048))
        retry = dict(initial.get("retry", {}))
        self._retry_max_attempts = int(retry.get("max_attempts", 6))
        self._retry_base_delay = float(retry.get("base_delay", 1.0))
        crypto = dict(initial.get("crypto", {}))
        self._crypto_enabled = bool(crypto.get("enabled", False))
        self._crypto_key_env = str(crypto.get("key_env", "TG_CRYPTO_KEY_B64"))

        # ── Resolve account chat_targets (read-only display) ─────────────
        self._accounts_repo = initial.get("_accounts_repo")
        account_channels = self._resolve_account_channels(self._accounts_repo)
        initial_main_channel_index = max(0, int(initial.get("main_channel_index", 0)))

        # ── Visible widgets ────────────────────────────────────────────────
        initial_api_id = int(initial.get("tg_api_id", 0) or 0)
        self.api_id_edit = QLineEdit(str(initial_api_id) if initial_api_id > 0 else "")
        self.api_id_edit.setPlaceholderText(self.tr("number from my.telegram.org/apps"))
        self.api_id_edit.setToolTip(
            self.tr(
                "Telegram app API ID (my.telegram.org/apps).\n"
                "If set in .env (TG_API_ID) — .env takes priority."
            )
        )
        self.api_hash_edit = QLineEdit(str(initial.get("tg_api_hash", "") or ""))
        self.api_hash_edit.setPlaceholderText(
            self.tr("32 hex characters from my.telegram.org/apps")
        )
        self.api_hash_edit.setToolTip(
            self.tr(
                "Telegram app API Hash (my.telegram.org/apps).\n"
                "If set in .env (TG_API_HASH) — .env takes priority."
            )
        )

        self.session_edit = QLineEdit(
            str(initial.get("tg_session_path", "./var/data/session.session"))
        )

        self.channels_label = QLabel(
            ", ".join(account_channels)
            if account_channels
            else self.tr("No accounts configured")
        )
        self.channels_label.setWordWrap(True)
        self.channels_label.setStyleSheet("color: #7a6fa0; font-size: 11px;")
        self.channels_label.setToolTip(
            self.tr(
                "Channels are configured per account in the Accounts window.\n"
                "Open it from the toolbar (the accounts icon) to add/edit them."
            )
        )

        self.main_route_spin = QSpinBox()
        self.main_route_spin.setRange(1, max(1, len(account_channels)))
        self.main_route_spin.setValue(initial_main_channel_index + 1)
        self.main_route_spin.setToolTip(
            self.tr("Number of the main channel from the list above (starting at 1).")
        )

        self.tg_proxy_edit = QLineEdit(str(initial.get("tg_proxy", "")))
        self.tg_proxy_edit.setPlaceholderText(
            self.tr("host:port:user:pass  (leave empty if not needed)")
        )
        self.tg_proxy_edit.setToolTip(
            self.tr(
                "Proxy for the main Telegram session (optional).\n"
                "SOCKS5/HTTP: host:port[:user:pass] or socks5://… / http://…\n"
                "MTProto: mtproto://HOST:PORT:SECRET or tg://proxy?server=…&secret=…"
            )
        )

        self.cache_edit = QLineEdit(str(initial.get("cache_dir", "./var/cache")))
        self.cache_edit.setToolTip(
            self.tr("Directory for temporary download cache files.")
        )

        self.download_edit = QLineEdit(str(initial.get("download_dir", "")))
        self.download_edit.setPlaceholderText(
            self.tr("Empty = save to the cache directory (as now)")
        )
        self.download_edit.setToolTip(
            self.tr(
                "Where to save downloaded files.\n"
                "If empty, files are placed in the cache directory."
            )
        )

        self.show_thumbs_chk = QCheckBox(self.tr("Image previews in the grid"))
        self.show_thumbs_chk.setChecked(bool(initial.get("show_thumbnails", True)))
        self.show_thumbs_chk.setToolTip(
            self.tr("Show thumbnails instead of icons for images.")
        )
        self.fetch_thumbs_chk = QCheckBox(
            self.tr("Fetch previews for not-yet-downloaded files")
        )
        self.fetch_thumbs_chk.setChecked(bool(initial.get("fetch_thumbnails", True)))
        self.fetch_thumbs_chk.setToolTip(
            self.tr(
                "Fetch not-yet-downloaded images in the background for previews "
                "(uses network traffic)."
            )
        )
        self.show_thumbs_chk.toggled.connect(self.fetch_thumbs_chk.setEnabled)
        self.fetch_thumbs_chk.setEnabled(self.show_thumbs_chk.isChecked())

        from PySide6.QtWidgets import QSlider, QWidget
        from PySide6.QtCore import Qt

        self.ui_icon_size_slider = QSlider(Qt.Orientation.Horizontal)
        self.ui_icon_size_slider.setRange(32, 256)
        self.ui_icon_size_slider.setValue(int(initial.get("ui_icon_size", 56)))
        self.ui_icon_size_slider.setToolTip(
            self.tr("Size of file and folder icons in pixels.")
        )

        self.ui_icon_size_label = QLabel(f"{self.ui_icon_size_slider.value()} px")
        self.ui_icon_size_label.setFixedWidth(50)
        self.ui_icon_size_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.ui_icon_size_slider.valueChanged.connect(
            lambda val: self.ui_icon_size_label.setText(f"{val} px")
        )

        icon_size_layout = QHBoxLayout()
        icon_size_layout.addWidget(self.ui_icon_size_slider)
        icon_size_layout.addWidget(self.ui_icon_size_label)

        self.icon_size_widget = QWidget()
        self.icon_size_widget.setLayout(icon_size_layout)
        icon_size_layout.setContentsMargins(0, 0, 0, 0)

        # ── REST API (increment 5) ─────────────────────────────────────────
        api_initial = initial.get("api", {}) or {}
        self.api_enabled_chk = QCheckBox(self.tr("Enable local REST API"))
        self.api_enabled_chk.setChecked(bool(api_initial.get("enabled", False)))
        self.api_enabled_chk.setToolTip(
            self.tr(
                "Local HTTP server on top of the core (list/upload/download/jobs).\n"
                "Applied after the application restarts."
            )
        )
        self.api_host_edit = QLineEdit(str(api_initial.get("host", "127.0.0.1")))
        self.api_host_edit.setToolTip(
            self.tr("127.0.0.1 — access only from this computer (recommended).")
        )
        self.api_port_spin = QSpinBox()
        self.api_port_spin.setRange(1, 65535)
        self.api_port_spin.setValue(int(api_initial.get("port", 20451)))
        self.api_token_edit = QLineEdit(str(api_initial.get("token", "")))
        self.api_token_edit.setPlaceholderText(self.tr("empty = no authorization"))
        self.api_token_edit.setToolTip(
            self.tr(
                "If set, requires the header Authorization: Bearer <token>.\n"
                "Empty = authorization disabled (relies on binding to 127.0.0.1)."
            )
        )
        for _w in (self.api_host_edit, self.api_port_spin, self.api_token_edit):
            _w.setEnabled(self.api_enabled_chk.isChecked())
        self.api_enabled_chk.toggled.connect(self.api_host_edit.setEnabled)
        self.api_enabled_chk.toggled.connect(self.api_port_spin.setEnabled)
        self.api_enabled_chk.toggled.connect(self.api_token_edit.setEnabled)

        self.conc_spin = QSpinBox()
        self.conc_spin.setRange(1, 16)
        self.conc_spin.setValue(int(initial.get("concurrency", 3)))
        self.conc_spin.setToolTip(
            self.tr("Parallel Telegram threads per task (2–4 recommended).")
        )

        self.max_jobs_spin = QSpinBox()
        self.max_jobs_spin.setRange(1, 16)
        self.max_jobs_spin.setValue(int(initial.get("max_active_jobs", 3)))
        self.max_jobs_spin.setToolTip(
            self.tr("Maximum number of simultaneous upload/download tasks.")
        )

        initial_part_size = max(
            int(initial.get("balanced_part_target_regular_mb", 256)),
            int(initial.get("balanced_part_target_premium_mb", 256)),
        )
        self.part_size_spin = QSpinBox()
        self.part_size_spin.setRange(64, 4096)
        self.part_size_spin.setSuffix(self.tr(" MB"))
        self.part_size_spin.setValue(initial_part_size)
        self.part_size_spin.setToolTip(
            self.tr(
                "Size of each file part in Telegram.\n"
                "Smaller = more parts, better parallelism.\n"
                "256 MB is a good value for 3 channels."
            )
        )

        # ── Advanced widgets (the "Advanced" tab) ─────────────────────
        self.integrity_combo = QComboBox()
        self.integrity_combo.addItem(self.tr("Strict (verify sha256)"), "strict")
        self.integrity_combo.addItem(self.tr("Fast (no full verification)"), "fast")
        self._select_combo_data(self.integrity_combo, self._download_integrity_mode)
        self.integrity_combo.setToolTip(
            self.tr("Strict is more reliable (verifies the checksum); fast is quicker.")
        )

        self.compression_combo = QComboBox()
        self.compression_combo.addItem(
            self.tr("Auto (compress what compresses)"), "auto"
        )
        self.compression_combo.addItem(self.tr("Off"), "off")
        self.compression_combo.addItem(self.tr("Force"), "force")
        self._select_combo_data(self.compression_combo, self._upload_compression_mode)
        self.compression_combo.setToolTip(
            self.tr(
                'Compression before upload. "Auto" compresses only what '
                "benefits from it."
            )
        )

        self.upload_safety_spin = QSpinBox()
        self.upload_safety_spin.setRange(0, 1024)
        self.upload_safety_spin.setSuffix(self.tr(" MB"))
        self.upload_safety_spin.setValue(self._upload_limit_safety_mb)
        self.upload_safety_spin.setToolTip(
            self.tr(
                "Margin below Telegram's file size limit "
                "(to avoid hitting the ceiling)."
            )
        )

        self.chunk_size_spin = QSpinBox()
        self.chunk_size_spin.setRange(1, 2048)
        self.chunk_size_spin.setSuffix(self.tr(" MB"))
        self.chunk_size_spin.setValue(self._chunk_size_mb)
        self.chunk_size_spin.setToolTip(
            self.tr("Read/encryption chunk size during upload. Affects memory usage.")
        )

        self.cache_limit_spin = QSpinBox()
        self.cache_limit_spin.setRange(0, 8_388_608)
        self.cache_limit_spin.setSuffix(self.tr(" MB"))
        self.cache_limit_spin.setSpecialValueText(self.tr("0 — no limit"))
        self.cache_limit_spin.setValue(self._cache_max_size_mb)
        self.cache_limit_spin.setToolTip(
            self.tr("Maximum download cache size. 0 — no limit.")
        )

        self.stream_cache_limit_spin = QSpinBox()
        self.stream_cache_limit_spin.setRange(0, 1_048_576)
        self.stream_cache_limit_spin.setSuffix(self.tr(" MB"))
        self.stream_cache_limit_spin.setSpecialValueText(self.tr("0 — no limit"))
        self.stream_cache_limit_spin.setValue(self._stream_cache_max_mb)
        self.stream_cache_limit_spin.setToolTip(
            self.tr(
                "Maximum size of the streaming parts cache (preview/share links).\n"
                "Old parts are evicted via LRU. 0 — no limit."
            )
        )

        self.small_threshold_spin = QSpinBox()
        self.small_threshold_spin.setRange(1, 65536)
        self.small_threshold_spin.setSuffix(self.tr(" KB"))
        self.small_threshold_spin.setValue(self._small_file_threshold_kb)
        self.small_threshold_spin.setToolTip(
            self.tr(
                "Files below this threshold are packed into a shared archive "
                "(batching)."
            )
        )

        self.small_batch_target_spin = QSpinBox()
        self.small_batch_target_spin.setRange(1, 512)
        self.small_batch_target_spin.setSuffix(self.tr(" MB"))
        self.small_batch_target_spin.setValue(self._small_file_batch_target_mb)
        self.small_batch_target_spin.setToolTip(
            self.tr("Target size of a single archive containing small files.")
        )

        self.send_rate_spin = QDoubleSpinBox()
        self.send_rate_spin.setRange(0.1, 100.0)
        self.send_rate_spin.setDecimals(1)
        self.send_rate_spin.setSingleStep(0.5)
        self.send_rate_spin.setValue(self._send_media_rate_limit)
        self.send_rate_spin.setToolTip(
            self.tr(
                "Telegram send request rate limit "
                "(higher = faster, but risk of flood-wait)."
            )
        )

        self.get_rate_spin = QDoubleSpinBox()
        self.get_rate_spin.setRange(0.1, 200.0)
        self.get_rate_spin.setDecimals(1)
        self.get_rate_spin.setSingleStep(0.5)
        self.get_rate_spin.setValue(self._get_file_rate_limit)
        self.get_rate_spin.setToolTip(
            self.tr(
                "Telegram download request rate limit "
                "(higher = faster, but risk of flood-wait)."
            )
        )

        self.upload_throttle_spin = QDoubleSpinBox()
        self.upload_throttle_spin.setRange(0.0, 10000.0)
        self.upload_throttle_spin.setDecimals(1)
        self.upload_throttle_spin.setSingleStep(1.0)
        self.upload_throttle_spin.setSuffix(self.tr(" MB/s"))
        self.upload_throttle_spin.setSpecialValueText(self.tr("0 — no limit"))
        self.upload_throttle_spin.setValue(
            float(initial.get("upload_throttle_mbps", 0.0))
        )
        self.upload_throttle_spin.setToolTip(
            self.tr("Upload bandwidth limit (outbound). 0 — no limit.")
        )

        self.download_throttle_spin = QDoubleSpinBox()
        self.download_throttle_spin.setRange(0.0, 10000.0)
        self.download_throttle_spin.setDecimals(1)
        self.download_throttle_spin.setSingleStep(1.0)
        self.download_throttle_spin.setSuffix(self.tr(" MB/s"))
        self.download_throttle_spin.setSpecialValueText(self.tr("0 — no limit"))
        self.download_throttle_spin.setValue(
            float(initial.get("download_throttle_mbps", 0.0))
        )
        self.download_throttle_spin.setToolTip(
            self.tr("Download bandwidth limit. 0 — no limit.")
        )

        self.retry_attempts_spin = QSpinBox()
        self.retry_attempts_spin.setRange(1, 20)
        self.retry_attempts_spin.setValue(self._retry_max_attempts)
        self.retry_attempts_spin.setToolTip(
            self.tr(
                "How many times to retry an operation on a transient "
                "network/Telegram error."
            )
        )

        self.retry_delay_spin = QDoubleSpinBox()
        self.retry_delay_spin.setRange(0.1, 60.0)
        self.retry_delay_spin.setDecimals(1)
        self.retry_delay_spin.setSingleStep(0.5)
        self.retry_delay_spin.setSuffix(self.tr(" s"))
        self.retry_delay_spin.setValue(self._retry_base_delay)
        self.retry_delay_spin.setToolTip(
            self.tr("Base delay before retrying (grows exponentially).")
        )

        self.crypto_enabled_chk = QCheckBox(self.tr("Encrypt content (AES-GCM)"))
        self.crypto_enabled_chk.setChecked(self._crypto_enabled)
        self.crypto_enabled_chk.setToolTip(
            self.tr(
                "Encrypt chunks before upload using a key from an environment "
                "variable.\n"
                "Changing this is NOT recommended once data has already been "
                "uploaded."
            )
        )
        self.crypto_key_env_edit = QLineEdit(self._crypto_key_env)
        self.crypto_key_env_edit.setToolTip(
            self.tr(
                "Name of the environment variable holding the base64 key (32 bytes)."
            )
        )
        self.crypto_key_env_edit.setEnabled(self.crypto_enabled_chk.isChecked())
        self.crypto_enabled_chk.toggled.connect(self.crypto_key_env_edit.setEnabled)

        # ── Layout ─────────────────────────────────────────────────────────
        browse_session = QPushButton("…")
        browse_session.setFixedWidth(32)
        browse_session.clicked.connect(self._choose_session)
        session_row = QHBoxLayout()
        session_row.setSpacing(6)
        session_row.addWidget(self.session_edit)
        session_row.addWidget(browse_session)

        browse_cache = QPushButton("…")
        browse_cache.setFixedWidth(32)
        browse_cache.clicked.connect(self._choose_cache_dir)
        cache_row = QHBoxLayout()
        cache_row.setSpacing(6)
        cache_row.addWidget(self.cache_edit)
        cache_row.addWidget(browse_cache)

        browse_download = QPushButton("…")
        browse_download.setFixedWidth(32)
        browse_download.clicked.connect(self._choose_download_dir)
        download_row = QHBoxLayout()
        download_row.setSpacing(6)
        download_row.addWidget(self.download_edit)
        download_row.addWidget(browse_download)

        # The "Basic" tab — what most people need to configure.
        basic_form = self._new_form()
        basic_form.addRow(self._section("TELEGRAM"))
        basic_form.addRow(self.tr("API ID"), self.api_id_edit)
        basic_form.addRow(self.tr("API Hash"), self.api_hash_edit)
        basic_form.addRow(self.tr("Session file"), session_row)
        basic_form.addRow(self.tr("Channels"), self.channels_label)
        basic_form.addRow(self.tr("Main channel #"), self.main_route_spin)
        basic_form.addRow(self.tr("Main proxy"), self.tg_proxy_edit)
        basic_form.addRow(self._section(self.tr("STORAGE")))
        basic_form.addRow(self.tr("Cache directory"), cache_row)
        basic_form.addRow(self.tr("Download folder"), download_row)
        basic_form.addRow(self._section(self.tr("INTERFACE")))
        basic_form.addRow(self.tr("Icon size"), self.icon_size_widget)
        basic_form.addRow("", self.show_thumbs_chk)
        basic_form.addRow("", self.fetch_thumbs_chk)
        basic_form.addRow(self._section(self.tr("PERFORMANCE")))
        basic_form.addRow(self.tr("Concurrency"), self.conc_spin)
        basic_form.addRow(self.tr("Max tasks"), self.max_jobs_spin)
        basic_form.addRow(self.tr("Part size"), self.part_size_spin)

        # The "Advanced" tab — fine-tuning for those who need it.
        adv_form = self._new_form()
        adv_form.addRow(self._section("REST API"))
        adv_form.addRow("", self.api_enabled_chk)
        adv_form.addRow(self.tr("Host"), self.api_host_edit)
        adv_form.addRow(self.tr("Port"), self.api_port_spin)
        adv_form.addRow(self.tr("Token"), self.api_token_edit)
        adv_form.addRow(self._section(self.tr("RELIABILITY")))
        adv_form.addRow(self.tr("Download integrity"), self.integrity_combo)
        adv_form.addRow(self.tr("Retries on error"), self.retry_attempts_spin)
        adv_form.addRow(self.tr("Retry delay"), self.retry_delay_spin)
        adv_form.addRow(self._section(self.tr("UPLOAD / DOWNLOAD")))
        adv_form.addRow(self.tr("Compression on upload"), self.compression_combo)
        adv_form.addRow(self.tr("Limit margin"), self.upload_safety_spin)
        adv_form.addRow(self.tr("Send limit (rps)"), self.send_rate_spin)
        adv_form.addRow(self.tr("Download limit (rps)"), self.get_rate_spin)
        adv_form.addRow(self.tr("Upload bandwidth"), self.upload_throttle_spin)
        adv_form.addRow(self.tr("Download bandwidth"), self.download_throttle_spin)
        adv_form.addRow(self._section(self.tr("STORAGE / CHUNKS")))
        adv_form.addRow(self.tr("Chunk size"), self.chunk_size_spin)
        adv_form.addRow(self.tr("Cache limit"), self.cache_limit_spin)
        adv_form.addRow(self.tr("Streaming cache limit"), self.stream_cache_limit_spin)
        adv_form.addRow(self._section(self.tr("SMALL FILES (batching)")))
        adv_form.addRow(self.tr("Small file threshold"), self.small_threshold_spin)
        adv_form.addRow(self.tr("Batch archive size"), self.small_batch_target_spin)
        adv_form.addRow(self._section(self.tr("ENCRYPTION")))
        adv_form.addRow("", self.crypto_enabled_chk)
        adv_form.addRow(self.tr("Key variable"), self.crypto_key_env_edit)

        tabs = QTabWidget()
        tabs.addTab(self._wrap_scroll(basic_form), self.tr("Basic"))
        tabs.addTab(self._wrap_scroll(adv_form), self.tr("Advanced"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(12)
        root.addWidget(tabs)
        root.addWidget(buttons)

        self.setMinimumWidth(540)
        self.setMinimumHeight(560)
        self._apply_theme()

    @staticmethod
    def _section(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color: #9b8fc0; font-size: 11px; font-weight: 700; "
            "letter-spacing: 0.5px; margin-top: 6px;"
        )
        return lbl

    @staticmethod
    def _new_form() -> QFormLayout:
        form = QFormLayout()
        form.setSpacing(10)
        form.setContentsMargins(4, 4, 4, 4)
        return form

    @staticmethod
    def _wrap_scroll(form: QFormLayout) -> QScrollArea:
        page = QWidget()
        page.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        return scroll

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: str) -> None:
        idx = combo.findData(value)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _apply_theme(self) -> None:
        self.setStyleSheet(_DIALOG_STYLESHEET)

    @staticmethod
    def _resolve_account_channels(repo) -> list[str]:
        """Read active account chat_targets from the database."""
        if repo is None:
            return []
        try:
            accounts = repo.list_accounts()
            return [
                a.chat_target for a in accounts if a.is_active and a.chat_target.strip()
            ]
        except Exception:
            return []

    def to_public_config(self) -> dict[str, Any]:
        channel_targets = self._resolve_account_channels(self._accounts_repo)
        raw_main_index = int(self.main_route_spin.value()) - 1
        main_channel_index = min(
            max(0, raw_main_index), max(0, len(channel_targets) - 1)
        )
        part_size = int(self.part_size_spin.value())
        max_jobs = int(self.max_jobs_spin.value())
        small_parallel = min(self._small_upload_parallel_jobs, max_jobs)
        try:
            api_id_value = int(self.api_id_edit.text().strip() or 0)
        except ValueError:
            api_id_value = 0
        return {
            "tg_api_id": api_id_value,
            "tg_api_hash": self.api_hash_edit.text().strip(),
            "tg_session_path": self.session_edit.text().strip(),
            "main_channel_index": main_channel_index,
            "channel_sharding_mode": "",
            "tg_proxy": self.tg_proxy_edit.text().strip(),
            "cache_dir": self.cache_edit.text().strip(),
            "download_dir": self.download_edit.text().strip(),
            "show_thumbnails": bool(self.show_thumbs_chk.isChecked()),
            "fetch_thumbnails": bool(self.fetch_thumbs_chk.isChecked()),
            "ui_icon_size": int(self.ui_icon_size_slider.value()),
            "chunk_size_mb": int(self.chunk_size_spin.value()),
            "concurrency": int(self.conc_spin.value()),
            "max_active_jobs": max_jobs,
            "cache_max_size_mb": int(self.cache_limit_spin.value()),
            "stream_cache_max_mb": int(self.stream_cache_limit_spin.value()),
            "caption_prefix": self._caption_prefix,
            "scan_search": self._scan_search,
            "use_sha_as_key": self._use_sha_as_key,
            "download_integrity_mode": str(self.integrity_combo.currentData()),
            "keep_partial_on_failure": True,
            "upload_compression_mode": str(self.compression_combo.currentData()),
            "upload_limit_safety_mb": int(self.upload_safety_spin.value()),
            "balanced_part_sizing_enabled": True,
            "balanced_part_min_file_mb": self._balanced_part_min_file_mb,
            "balanced_part_target_regular_mb": part_size,
            "balanced_part_target_premium_mb": part_size,
            "small_file_batching_enabled": True,
            "small_file_threshold_kb": int(self.small_threshold_spin.value()),
            "small_file_batch_target_mb": int(self.small_batch_target_spin.value()),
            "small_upload_parallel_jobs": small_parallel,
            "small_batch_mode": self._small_batch_mode,
            "small_batch_max_files": self._small_batch_max_files,
            "small_batch_manifest_mode": self._small_batch_manifest_mode,
            "send_media_rate_limit": float(self.send_rate_spin.value()),
            "get_file_rate_limit": float(self.get_rate_spin.value()),
            "upload_throttle_mbps": float(self.upload_throttle_spin.value()),
            "download_throttle_mbps": float(self.download_throttle_spin.value()),
            "lane_upload_small_max": self._lane_upload_small_max,
            "lane_upload_large_max": self._lane_upload_large_max,
            "lane_download_max": self._lane_download_max,
            "perf_telemetry_window_sec": self._perf_telemetry_window_sec,
            "retry": {
                "max_attempts": int(self.retry_attempts_spin.value()),
                "base_delay": float(self.retry_delay_spin.value()),
            },
            "crypto": {
                "enabled": bool(self.crypto_enabled_chk.isChecked()),
                "key_env": self.crypto_key_env_edit.text().strip()
                or "TG_CRYPTO_KEY_B64",
            },
            "api": {
                "enabled": bool(self.api_enabled_chk.isChecked()),
                "host": self.api_host_edit.text().strip() or "127.0.0.1",
                "port": int(self.api_port_spin.value()),
                "token": self.api_token_edit.text().strip(),
            },
        }

    def _choose_session(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Choose session file"), self.session_edit.text()
        )
        if path:
            self.session_edit.setText(path)

    def _choose_cache_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, self.tr("Choose cache directory"), self.cache_edit.text()
        )
        if path:
            self.cache_edit.setText(path)

    def _choose_download_dir(self) -> None:
        start_dir = self.download_edit.text().strip() or self.cache_edit.text()
        path = QFileDialog.getExistingDirectory(
            self, self.tr("Choose folder for downloaded files"), start_dir
        )
        if path:
            self.download_edit.setText(path)

    @staticmethod
    def _split_list_field(raw: str) -> list[str]:
        return [
            part.strip()
            for part in str(raw or "").replace(";", ",").split(",")
            if part.strip()
        ]

    @staticmethod
    def _join_list_field(items: Any) -> str:
        if not isinstance(items, list):
            return ""
        return ", ".join(str(item).strip() for item in items if str(item).strip())


class SettingsDialog(SetupDialog):
    def __init__(self, initial: dict[str, Any] | None = None, parent=None) -> None:
        super().__init__(initial=initial, parent=parent)
        self.setWindowTitle(self.tr("Settings"))


class CreateFolderDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Create Folder"))

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(self.tr("New folder"))

        form = QFormLayout()
        form.addRow(self.tr("Name"), self.name_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.addLayout(form)
        root.addWidget(buttons)
        self.setStyleSheet(_DIALOG_STYLESHEET)

    def folder_name(self) -> str:
        return self.name_edit.text().strip()


class RenameDialog(QDialog):
    def __init__(self, current_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Rename"))

        self.name_edit = QLineEdit(current_name)
        self.name_edit.selectAll()

        form = QFormLayout()
        form.addRow(self.tr("New name"), self.name_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.addLayout(form)
        root.addWidget(buttons)
        self.setStyleSheet(_DIALOG_STYLESHEET)

    def new_name(self) -> str:
        return self.name_edit.text().strip()


class ConfirmDialog(QDialog):
    """A polished custom dialog for confirming actions."""

    def __init__(
        self, title: str, message: str, parent=None, is_destructive: bool = False
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)

        self.setStyleSheet("""
            QDialog {
                background-color: #09090b;
            }
            QLabel {
                color: #e4e4e7;
                font-size: 14px;
            }
            QPushButton {
                background-color: #27272a;
                color: #e4e4e7;
                border: 1px solid #3f3f46;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 500;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #3f3f46;
                color: #ffffff;
            }
            QPushButton#primaryBtn {
                background-color: #6d28d9;
                border-color: #5b21b6;
                color: #ffffff;
            }
            QPushButton#primaryBtn:hover {
                background-color: #7c3aed;
            }
            QPushButton#destructiveBtn {
                color: #fca5a5;
                border-color: #7f1d1d;
                background-color: #450a0a;
            }
            QPushButton#destructiveBtn:hover {
                background-color: #7f1d1d;
                color: #fef2f2;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)

        # Message
        msg_label = QLabel(message)
        msg_label.setWordWrap(True)
        layout.addWidget(msg_label)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.btn_cancel = QPushButton(self.tr("Cancel"))
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_confirm = QPushButton(self.tr("Yes"))
        self.btn_confirm.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_confirm.setDefault(True)
        self.btn_confirm.setAutoDefault(True)
        if is_destructive:
            self.btn_confirm.setObjectName("destructiveBtn")
        else:
            self.btn_confirm.setObjectName("primaryBtn")
        self.btn_confirm.clicked.connect(self.accept)
        btn_layout.addWidget(self.btn_confirm)

        layout.addLayout(btn_layout)


def ask_confirm_incomplete_download(parent) -> bool:
    dialog = ConfirmDialog(
        title=QApplication.translate("ConfirmDialog", "Incomplete File"),
        message=QApplication.translate(
            "ConfirmDialog",
            "The file was not downloaded completely. "
            "Download the available parts anyway?",
        ),
        parent=parent,
        is_destructive=False,
    )
    return dialog.exec() == QDialog.DialogCode.Accepted
