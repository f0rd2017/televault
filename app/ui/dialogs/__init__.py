from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
        self.setWindowTitle("Начальная настройка")
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
        self.session_edit = QLineEdit(
            str(initial.get("tg_session_path", "./var/data/session.session"))
        )

        self.channels_label = QLabel(
            ", ".join(account_channels) if account_channels else "Аккаунты не настроены"
        )
        self.channels_label.setWordWrap(True)
        self.channels_label.setStyleSheet("color: #7a6fa0; font-size: 11px;")
        self.channels_label.setToolTip(
            "Каналы настраиваются для каждого аккаунта в диалоге Аккаунты.\n"
            "Откройте Настройки > Аккаунты для добавления/редактирования."
        )

        self.main_route_spin = QSpinBox()
        self.main_route_spin.setRange(1, 4)
        self.main_route_spin.setValue(initial_main_channel_index + 1)
        self.main_route_spin.setToolTip(
            "Номер основного канала из списка выше (начиная с 1)."
        )

        self.tg_proxy_edit = QLineEdit(str(initial.get("tg_proxy", "")))
        self.tg_proxy_edit.setPlaceholderText(
            "host:port:user:pass  (оставьте пустым если не нужен)"
        )
        self.tg_proxy_edit.setToolTip(
            "Прокси для основной сессии Telegram (необязательно).\n"
            "SOCKS5/HTTP: host:port[:user:pass] или socks5://… / http://…\n"
            "MTProto: mtproto://HOST:PORT:SECRET или tg://proxy?server=…&secret=…"
        )

        self.cache_edit = QLineEdit(str(initial.get("cache_dir", "./var/cache")))
        self.cache_edit.setToolTip("Директория для временных файлов кэша скачивания.")

        self.download_edit = QLineEdit(str(initial.get("download_dir", "")))
        self.download_edit.setPlaceholderText(
            "Пусто = сохранять в директорию кэша (как сейчас)"
        )
        self.download_edit.setToolTip(
            "Куда сохранять скачанные файлы.\n"
            "Если поле пустое — файлы кладутся в директорию кэша."
        )

        self.show_thumbs_chk = QCheckBox("Превью картинок в гриде")
        self.show_thumbs_chk.setChecked(bool(initial.get("show_thumbnails", True)))
        self.show_thumbs_chk.setToolTip(
            "Показывать миниатюры вместо иконок для картинок."
        )
        self.fetch_thumbs_chk = QCheckBox("Тянуть превью для нескачанных")
        self.fetch_thumbs_chk.setChecked(bool(initial.get("fetch_thumbnails", True)))
        self.fetch_thumbs_chk.setToolTip(
            "Фоном дозагружать ещё не скачанные картинки ради превью "
            "(использует трафик)."
        )
        self.show_thumbs_chk.toggled.connect(self.fetch_thumbs_chk.setEnabled)
        self.fetch_thumbs_chk.setEnabled(self.show_thumbs_chk.isChecked())

        from PySide6.QtWidgets import QSlider, QWidget
        from PySide6.QtCore import Qt

        self.ui_icon_size_slider = QSlider(Qt.Orientation.Horizontal)
        self.ui_icon_size_slider.setRange(32, 256)
        self.ui_icon_size_slider.setValue(int(initial.get("ui_icon_size", 56)))
        self.ui_icon_size_slider.setToolTip("Размер иконок файлов и папок в пикселях.")

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

        # ── REST API (инкремент 5) ─────────────────────────────────────────
        api_initial = initial.get("api", {}) or {}
        self.api_enabled_chk = QCheckBox("Включить локальный REST API")
        self.api_enabled_chk.setChecked(bool(api_initial.get("enabled", False)))
        self.api_enabled_chk.setToolTip(
            "Локальный HTTP-сервер поверх ядра (список/загрузка/скачивание/джобы).\n"
            "Применяется после перезапуска приложения."
        )
        self.api_host_edit = QLineEdit(str(api_initial.get("host", "127.0.0.1")))
        self.api_host_edit.setToolTip(
            "127.0.0.1 — доступ только с этого компьютера (рекомендуется)."
        )
        self.api_port_spin = QSpinBox()
        self.api_port_spin.setRange(1, 65535)
        self.api_port_spin.setValue(int(api_initial.get("port", 20451)))
        self.api_token_edit = QLineEdit(str(api_initial.get("token", "")))
        self.api_token_edit.setPlaceholderText("пусто = без авторизации")
        self.api_token_edit.setToolTip(
            "Если задан — нужен заголовок Authorization: Bearer <токен>.\n"
            "Пусто = авторизация отключена (полагается на привязку к 127.0.0.1)."
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
            "Параллельные потоки Telegram на задачу (рекомендуется 2–4)."
        )

        self.max_jobs_spin = QSpinBox()
        self.max_jobs_spin.setRange(1, 16)
        self.max_jobs_spin.setValue(int(initial.get("max_active_jobs", 3)))
        self.max_jobs_spin.setToolTip(
            "Максимум одновременных задач загрузки/скачивания."
        )

        initial_part_size = max(
            int(initial.get("balanced_part_target_regular_mb", 256)),
            int(initial.get("balanced_part_target_premium_mb", 256)),
        )
        self.part_size_spin = QSpinBox()
        self.part_size_spin.setRange(64, 4096)
        self.part_size_spin.setSuffix(" МБ")
        self.part_size_spin.setValue(initial_part_size)
        self.part_size_spin.setToolTip(
            "Размер каждой части файла в Telegram.\n"
            "Меньше = больше частей, лучше параллелизм.\n"
            "256 МБ — хорошее значение для 3 каналов."
        )

        # ── Расширенные виджеты (вкладка «Расширенные») ─────────────────────
        self.integrity_combo = QComboBox()
        self.integrity_combo.addItem("Строгая (проверять sha256)", "strict")
        self.integrity_combo.addItem("Быстрая (без полной сверки)", "fast")
        self._select_combo_data(self.integrity_combo, self._download_integrity_mode)
        self.integrity_combo.setToolTip(
            "Строгая надёжнее (сверяет контрольную сумму), быстрая — быстрее."
        )

        self.compression_combo = QComboBox()
        self.compression_combo.addItem("Авто (сжимать сжимаемое)", "auto")
        self.compression_combo.addItem("Выключено", "off")
        self.compression_combo.addItem("Принудительно", "force")
        self._select_combo_data(self.compression_combo, self._upload_compression_mode)
        self.compression_combo.setToolTip(
            "Сжатие перед загрузкой. «Авто» — сжимать только то, что сжимается."
        )

        self.upload_safety_spin = QSpinBox()
        self.upload_safety_spin.setRange(0, 1024)
        self.upload_safety_spin.setSuffix(" МБ")
        self.upload_safety_spin.setValue(self._upload_limit_safety_mb)
        self.upload_safety_spin.setToolTip(
            "Запас от лимита Telegram на размер файла (чтобы не упереться в потолок)."
        )

        self.chunk_size_spin = QSpinBox()
        self.chunk_size_spin.setRange(1, 2048)
        self.chunk_size_spin.setSuffix(" МБ")
        self.chunk_size_spin.setValue(self._chunk_size_mb)
        self.chunk_size_spin.setToolTip(
            "Размер чанка чтения/шифрования при загрузке. Влияет на память."
        )

        self.cache_limit_spin = QSpinBox()
        self.cache_limit_spin.setRange(0, 8_388_608)
        self.cache_limit_spin.setSuffix(" МБ")
        self.cache_limit_spin.setSpecialValueText("0 — без лимита")
        self.cache_limit_spin.setValue(self._cache_max_size_mb)
        self.cache_limit_spin.setToolTip(
            "Максимальный размер кэша скачивания. 0 — без ограничения."
        )

        self.stream_cache_limit_spin = QSpinBox()
        self.stream_cache_limit_spin.setRange(0, 1_048_576)
        self.stream_cache_limit_spin.setSuffix(" МБ")
        self.stream_cache_limit_spin.setSpecialValueText("0 — без лимита")
        self.stream_cache_limit_spin.setValue(self._stream_cache_max_mb)
        self.stream_cache_limit_spin.setToolTip(
            "Максимальный размер кэша частей стриминга (просмотр/шар-ссылки).\n"
            "Старые части вытесняются по LRU. 0 — без ограничения."
        )

        self.small_threshold_spin = QSpinBox()
        self.small_threshold_spin.setRange(1, 65536)
        self.small_threshold_spin.setSuffix(" КБ")
        self.small_threshold_spin.setValue(self._small_file_threshold_kb)
        self.small_threshold_spin.setToolTip(
            "Файлы меньше этого порога пакуются в общий архив (батчинг)."
        )

        self.small_batch_target_spin = QSpinBox()
        self.small_batch_target_spin.setRange(1, 512)
        self.small_batch_target_spin.setSuffix(" МБ")
        self.small_batch_target_spin.setValue(self._small_file_batch_target_mb)
        self.small_batch_target_spin.setToolTip(
            "Целевой размер одного архива с мелкими файлами."
        )

        self.send_rate_spin = QDoubleSpinBox()
        self.send_rate_spin.setRange(0.1, 100.0)
        self.send_rate_spin.setDecimals(1)
        self.send_rate_spin.setSingleStep(0.5)
        self.send_rate_spin.setValue(self._send_media_rate_limit)
        self.send_rate_spin.setToolTip(
            "Лимит запросов отправки в Telegram (выше = быстрее, но риск flood-wait)."
        )

        self.get_rate_spin = QDoubleSpinBox()
        self.get_rate_spin.setRange(0.1, 200.0)
        self.get_rate_spin.setDecimals(1)
        self.get_rate_spin.setSingleStep(0.5)
        self.get_rate_spin.setValue(self._get_file_rate_limit)
        self.get_rate_spin.setToolTip(
            "Лимит запросов скачивания из Telegram (выше = быстрее, но риск flood-wait)."
        )

        self.upload_throttle_spin = QDoubleSpinBox()
        self.upload_throttle_spin.setRange(0.0, 10000.0)
        self.upload_throttle_spin.setDecimals(1)
        self.upload_throttle_spin.setSingleStep(1.0)
        self.upload_throttle_spin.setSuffix(" МБ/с")
        self.upload_throttle_spin.setSpecialValueText("0 — без лимита")
        self.upload_throttle_spin.setValue(
            float(initial.get("upload_throttle_mbps", 0.0))
        )
        self.upload_throttle_spin.setToolTip(
            "Ограничение полосы загрузки (отдачи). 0 — без лимита."
        )

        self.download_throttle_spin = QDoubleSpinBox()
        self.download_throttle_spin.setRange(0.0, 10000.0)
        self.download_throttle_spin.setDecimals(1)
        self.download_throttle_spin.setSingleStep(1.0)
        self.download_throttle_spin.setSuffix(" МБ/с")
        self.download_throttle_spin.setSpecialValueText("0 — без лимита")
        self.download_throttle_spin.setValue(
            float(initial.get("download_throttle_mbps", 0.0))
        )
        self.download_throttle_spin.setToolTip(
            "Ограничение полосы скачивания. 0 — без лимита."
        )

        self.retry_attempts_spin = QSpinBox()
        self.retry_attempts_spin.setRange(1, 20)
        self.retry_attempts_spin.setValue(self._retry_max_attempts)
        self.retry_attempts_spin.setToolTip(
            "Сколько раз повторять операцию при временной ошибке сети/Telegram."
        )

        self.retry_delay_spin = QDoubleSpinBox()
        self.retry_delay_spin.setRange(0.1, 60.0)
        self.retry_delay_spin.setDecimals(1)
        self.retry_delay_spin.setSingleStep(0.5)
        self.retry_delay_spin.setSuffix(" с")
        self.retry_delay_spin.setValue(self._retry_base_delay)
        self.retry_delay_spin.setToolTip(
            "Базовая задержка перед повтором (растёт экспоненциально)."
        )

        self.crypto_enabled_chk = QCheckBox("Шифровать содержимое (AES-GCM)")
        self.crypto_enabled_chk.setChecked(self._crypto_enabled)
        self.crypto_enabled_chk.setToolTip(
            "Шифровать чанки перед загрузкой ключом из переменной окружения.\n"
            "Менять при наличии уже загруженных данных НЕ рекомендуется."
        )
        self.crypto_key_env_edit = QLineEdit(self._crypto_key_env)
        self.crypto_key_env_edit.setToolTip(
            "Имя переменной окружения с base64-ключом (32 байта)."
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

        # Вкладка «Основные» — то, что нужно настроить большинству.
        basic_form = self._new_form()
        basic_form.addRow(self._section("TELEGRAM"))
        basic_form.addRow("Файл сессии", session_row)
        basic_form.addRow("Каналы", self.channels_label)
        basic_form.addRow("Основной канал №", self.main_route_spin)
        basic_form.addRow("Основной прокси", self.tg_proxy_edit)
        basic_form.addRow(self._section("ХРАНИЛИЩЕ"))
        basic_form.addRow("Директория кэша", cache_row)
        basic_form.addRow("Папка скачивания", download_row)
        basic_form.addRow(self._section("ИНТЕРФЕЙС"))
        basic_form.addRow("Размер значков", self.icon_size_widget)
        basic_form.addRow("", self.show_thumbs_chk)
        basic_form.addRow("", self.fetch_thumbs_chk)
        basic_form.addRow(self._section("ПРОИЗВОДИТЕЛЬНОСТЬ"))
        basic_form.addRow("Параллельность", self.conc_spin)
        basic_form.addRow("Макс. задач", self.max_jobs_spin)
        basic_form.addRow("Размер частей", self.part_size_spin)

        # Вкладка «Расширенные» — тонкая настройка для тех, кому надо.
        adv_form = self._new_form()
        adv_form.addRow(self._section("REST API"))
        adv_form.addRow("", self.api_enabled_chk)
        adv_form.addRow("Хост", self.api_host_edit)
        adv_form.addRow("Порт", self.api_port_spin)
        adv_form.addRow("Токен", self.api_token_edit)
        adv_form.addRow(self._section("НАДЁЖНОСТЬ"))
        adv_form.addRow("Целостность скачивания", self.integrity_combo)
        adv_form.addRow("Повторов при ошибке", self.retry_attempts_spin)
        adv_form.addRow("Задержка повтора", self.retry_delay_spin)
        adv_form.addRow(self._section("ЗАГРУЗКА / СКАЧИВАНИЕ"))
        adv_form.addRow("Сжатие при загрузке", self.compression_combo)
        adv_form.addRow("Запас от лимита", self.upload_safety_spin)
        adv_form.addRow("Лимит отправки (rps)", self.send_rate_spin)
        adv_form.addRow("Лимит скачивания (rps)", self.get_rate_spin)
        adv_form.addRow("Полоса загрузки", self.upload_throttle_spin)
        adv_form.addRow("Полоса скачивания", self.download_throttle_spin)
        adv_form.addRow(self._section("ХРАНИЛИЩЕ / ЧАНКИ"))
        adv_form.addRow("Размер чанка", self.chunk_size_spin)
        adv_form.addRow("Лимит кэша", self.cache_limit_spin)
        adv_form.addRow("Лимит кэша стриминга", self.stream_cache_limit_spin)
        adv_form.addRow(self._section("МЕЛКИЕ ФАЙЛЫ (батчинг)"))
        adv_form.addRow("Порог мелкого файла", self.small_threshold_spin)
        adv_form.addRow("Размер архива батча", self.small_batch_target_spin)
        adv_form.addRow(self._section("ШИФРОВАНИЕ"))
        adv_form.addRow("", self.crypto_enabled_chk)
        adv_form.addRow("Переменная ключа", self.crypto_key_env_edit)

        tabs = QTabWidget()
        tabs.addTab(self._wrap_scroll(basic_form), "Основные")
        tabs.addTab(self._wrap_scroll(adv_form), "Расширенные")

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
        return {
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
            self, "Выберите файл сессии", self.session_edit.text()
        )
        if path:
            self.session_edit.setText(path)

    def _choose_cache_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Выберите директорию кэша", self.cache_edit.text()
        )
        if path:
            self.cache_edit.setText(path)

    def _choose_download_dir(self) -> None:
        start_dir = self.download_edit.text().strip() or self.cache_edit.text()
        path = QFileDialog.getExistingDirectory(
            self, "Выберите папку для скачанных файлов", start_dir
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
        self.setWindowTitle("Настройки")


class CreateFolderDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Создать папку")

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Новая папка")

        form = QFormLayout()
        form.addRow("Имя", self.name_edit)

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
        self.setWindowTitle("Переименовать")

        self.name_edit = QLineEdit(current_name)
        self.name_edit.selectAll()

        form = QFormLayout()
        form.addRow("Новое имя", self.name_edit)

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
    """Красивое кастомное диалоговое окно для подтверждения действий."""

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

        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_confirm = QPushButton("Да")
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
        title="Неполный файл",
        message="Файл загружен не полностью. Скачать доступные части всё равно?",
        parent=parent,
        is_destructive=False,
    )
    return dialog.exec() == QDialog.DialogCode.Accepted
