from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QModelIndex, QSize, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedLayout,
    QSystemTrayIcon,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import (
    available_languages,
    install_language,
    language_by_code,
    save_language,
    saved_language,
)
from app.core.types import AppConfig, JobEvent
from app.core.worker import TelegramWorker
from app.db.repo import DbRepo
from app.ui.job_toasts import JobToastCard, JobToastOverlay
from app.ui.models_qt import ExplorerGridModel, FolderTreeModel
from app.ui.widgets import ProgressLogWidget
from app.ui.panels import (
    ExplorerDropFrame,
    ExplorerListView,
    ExplorerPanelMixin,
    FolderPanelMixin,
    JobEventsMixin,
    MiscMixin,
    TransferOpsMixin,
    UploadDropMixin,
)

try:
    import qtawesome as qta
except (
    Exception
):  # pragma: no cover - graceful fallback when optional runtime dep is unavailable
    qta = None


class MainWindow(
    FolderPanelMixin,
    ExplorerPanelMixin,
    UploadDropMixin,
    TransferOpsMixin,
    JobEventsMixin,
    MiscMixin,
    QMainWindow,
):
    _LOCAL_PRESENCE_CACHE_TTL_SEC = 2.0
    _LOCAL_PRESENCE_CACHE_MAX = 4096
    _EAGER_LOCAL_PRESENCE_LIMIT = 320
    _RELOAD_DEBOUNCE_MS = 260
    _ERROR_DIALOG_DEBOUNCE_MS = 350
    _STALE_JOB_SECONDS = 45.0
    _WATCHDOG_INTERVAL_MS = 2_000
    _FOLDER_SEGMENT_MAX_LEN = 64
    _FOLDER_SEGMENT_HASH_LEN = 8
    _ENQUEUE_RETRY_INTERVAL_MS = 350
    _ENQUEUE_RETRY_MAX_ATTEMPTS = 32

    def __init__(
        self,
        config: AppConfig,
        repo: DbRepo,
        worker: TelegramWorker,
        save_config_callback: Callable[[dict], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Telegram Cloud Cache Manager")
        self.resize(1440, 920)

        self.config = config
        self.repo = repo
        self.worker = worker
        self.save_config_callback = save_config_callback

        self.folder_model = FolderTreeModel(self)
        self.explorer_model = ExplorerGridModel(
            self,
            thumb_cache_dir=str(Path(config.cache_dir).expanduser() / ".thumb_cache"),
        )

        self.current_folder: str | None = None
        self._all_folders: list[str] = []
        self._history: list[str | None] = [None]
        self._history_index = 0

        self._pending_upload_jobs: list[dict[str, Any]] = []
        self._job_log_bucket: dict[int, int] = {}
        self._local_presence_cache: dict[str, tuple[float, bool]] = {}
        self._lazy_presence_mode = False
        self._trash_view = False  # "Trash" mode (soft-deleted objects)
        # Image previews: lazily fetch not-yet-downloaded thumbnails into a temp dir.
        self._thumb_fetch_dir = str(
            Path(config.cache_dir).expanduser() / ".thumb_fetch"
        )
        self._thumb_fetch_inflight: set[tuple[str, str]] = set()
        self._thumb_fetch_failed: set[tuple[str, str]] = set()
        self._THUMB_FETCH_MAX_INFLIGHT = 2
        self._cleanup_thumbnail_dirs_async()
        self._pending_error_events: list[tuple[int, str, str]] = []
        self._job_last_update_ts: dict[int, float] = {}
        self._stale_notified_jobs: set[int] = set()
        self._running_jobs: set[int] = set()
        self._batch_state_by_id: dict[str, dict[str, Any]] = {}
        self._batch_toast_by_id: dict[str, JobToastCard] = {}
        self._pending_enqueue_retries: dict[str, dict[str, Any]] = {}

        # Active jobs/toasts tracking.
        self._active_jobs: set[int] = set()
        self._job_progress: dict[int, float] = {}
        self._job_progress_weight: dict[int, float] = {}
        self._job_type_by_id: dict[int, str] = {}
        self._pending_running_events: dict[int, JobEvent] = {}
        self._toast_by_job_id: dict[int, JobToastCard] = {}
        self._toast_by_request_id: dict[str, JobToastCard] = {}
        self._inflight_requests: set[str] = set()
        self._inflight_request_meta: dict[str, dict[str, Any]] = {}
        self._finished_transfer_total_bytes = 0.0
        self._finished_transfer_done_bytes = 0.0
        self._finalized_transfer_jobs: set[int] = set()
        # Speed/ETA estimate: a ring buffer of (monotonic_ts, done_bytes) samples
        # over the last _ETA_WINDOW_SEC seconds. Speed is computed over a real
        # time window rather than event count — job events arrive in bursts,
        # which would otherwise make the numbers jump around.
        self._eta_samples: deque[tuple[float, float]] = deque()
        self._eta_display_speed_bps = 0.0

        self._local_presence_timer = QTimer(self)
        self._local_presence_timer.setInterval(450)
        self._local_presence_timer.timeout.connect(self._refresh_visible_local_presence)
        self._running_event_flush_timer = QTimer(self)
        self._running_event_flush_timer.setInterval(300)
        self._running_event_flush_timer.timeout.connect(
            self._flush_pending_running_event
        )
        self._loading_badge_timer = QTimer(self)
        self._loading_badge_timer.setInterval(500)
        self._loading_badge_timer.timeout.connect(self._advance_loading_badges)
        self._stream_cleanup_timer = QTimer(self)
        self._stream_cleanup_timer.setInterval(10 * 60 * 1000)
        self._stream_cleanup_timer.timeout.connect(self._run_stream_cleanup)
        self._search_debounce_timer = QTimer(self)
        self._search_debounce_timer.setSingleShot(True)
        self._search_debounce_timer.setInterval(220)
        self._search_debounce_timer.timeout.connect(self.reload_items)
        self._reload_debounce_timer = QTimer(self)
        self._reload_debounce_timer.setSingleShot(True)
        self._reload_debounce_timer.setInterval(self._RELOAD_DEBOUNCE_MS)
        self._reload_debounce_timer.timeout.connect(self._perform_scheduled_reload)
        self._error_dialog_timer = QTimer(self)
        self._error_dialog_timer.setSingleShot(True)
        self._error_dialog_timer.setInterval(self._ERROR_DIALOG_DEBOUNCE_MS)
        self._error_dialog_timer.timeout.connect(self._flush_error_dialogs)
        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setInterval(self._WATCHDOG_INTERVAL_MS)
        self._watchdog_timer.timeout.connect(self._check_stalled_jobs)
        self._enqueue_retry_timer = QTimer(self)
        self._enqueue_retry_timer.setInterval(self._ENQUEUE_RETRY_INTERVAL_MS)
        self._enqueue_retry_timer.timeout.connect(self._process_pending_enqueue_retries)
        self._reload_requested = False
        self._shutdown_started = False

        self._shortcuts: list = []

        self.setAcceptDrops(True)
        self._build_ui()
        self._wire_events()
        self._apply_dark_theme()

        # Toast overlay (created after _build_ui so centralWidget exists)
        self._toast_overlay = JobToastOverlay(self.centralWidget())

        # Startup loading overlay — shown until the worker connects (or fails).
        from app.ui.widgets import StartupLoadingOverlay

        self._startup_overlay = StartupLoadingOverlay(self.centralWidget())
        self._startup_overlay.retry_requested.connect(self._on_reconnect)
        self._startup_overlay.accounts_requested.connect(self._on_accounts)
        self._startup_overlay.setGeometry(self.centralWidget().rect())
        self._startup_overlay.show_loading(self.tr("Connecting to Telegram…"))

        # System tray
        self._tray = QSystemTrayIcon(self)
        self._tray_menu = QMenu()
        self._tray_action_show = self._tray_menu.addAction(self.tr("Show"), self.show)
        self._tray_action_quit = self._tray_menu.addAction(
            self.tr("Quit"), QApplication.quit
        )
        self._tray.setContextMenu(self._tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

        self.worker.job_event.connect(self._on_job_event)
        self.worker.ready.connect(self._on_worker_ready)
        self.worker.fatal_error.connect(self._on_worker_fatal_error)
        self.worker.reconnect_attempt.connect(self._on_worker_reconnect_attempt)
        self.worker.account_pool_status.connect(self._on_account_pool_status)
        self.worker.thumbnail_ready.connect(self._on_thumbnail_ready)
        self.worker.thumbnail_failed.connect(self._on_thumbnail_failed)
        self._local_presence_timer.start()
        self._stream_cleanup_timer.start()
        self.reload_all()

    def _build_ui(self) -> None:
        self.action_settings = QAction(self.tr("Settings"), self)
        self.action_reconnect = QAction(self.tr("Reconnect"), self)
        self.action_reconnect.setEnabled(False)
        self.action_accounts = QAction(self.tr("Telegram Accounts"), self)

        # Standalone actions (used in context menus, not in the toolbar)
        self.action_create_folder = QAction(self.tr("Create folder"), self)
        self.action_upload = QAction(self.tr("Upload"), self)
        self.action_download = QAction(self.tr("Download"), self)
        self.action_download_folder = QAction(self.tr("Download folder"), self)
        self.action_delete_local = QAction(self.tr("Delete locally"), self)
        self.action_delete = QAction(self.tr("Delete remotely"), self)
        self.action_refresh = QAction(self.tr("Refresh"), self)
        self.action_reconcile = QAction(self.tr("Reconcile database"), self)
        self.action_reindex = QAction(self.tr("Full reindex"), self)

        central = QWidget(self)
        central.setObjectName("mainCentral")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        top_bar_layout = QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(12, 10, 12, 10)
        top_bar_layout.setSpacing(10)

        self.nav_back_btn = QPushButton("‹")
        self.nav_forward_btn = QPushButton("›")
        self.nav_up_btn = QPushButton("⌂")
        for btn in (self.nav_back_btn, self.nav_forward_btn, self.nav_up_btn):
            btn.setFixedSize(36, 36)
            btn.setObjectName("navButton")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            top_bar_layout.addWidget(btn)
        self.nav_back_btn.setToolTip(self.tr("Back (Alt+Left)"))
        self.nav_forward_btn.setToolTip(self.tr("Forward (Alt+Right)"))
        self.nav_up_btn.setToolTip(self.tr("Up (Alt+Up)"))

        self.path_bar = QLineEdit()
        self.path_bar.setObjectName("pathBar")
        self.path_bar.setReadOnly(True)
        self.path_bar.setMinimumHeight(36)
        self.path_bar.setPlaceholderText(self.tr("Cloud path"))
        self.path_bar.setToolTip(self.tr("Current path"))
        top_bar_layout.addWidget(self.path_bar, 1)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchEdit")
        self.search_edit.setPlaceholderText(self.tr("Search by file name"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumHeight(36)
        self.search_edit.setFixedWidth(320)
        self.search_edit.setToolTip(
            self.tr("Search files in the current folder (Ctrl+F)")
        )
        top_bar_layout.addWidget(self.search_edit)

        self.search_everywhere_btn = QPushButton(self.tr("Everywhere"))
        self.search_everywhere_btn.setObjectName("topActionButton")
        self.search_everywhere_btn.setCheckable(True)
        self.search_everywhere_btn.setFixedSize(64, 36)
        self.search_everywhere_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_everywhere_btn.setToolTip(
            self.tr("Search all nested folders (recursively), not just the current one")
        )
        top_bar_layout.addWidget(self.search_everywhere_btn)

        self.trash_btn = QPushButton("🗑")
        self.trash_btn.setObjectName("topActionButton")
        self.trash_btn.setCheckable(True)
        self.trash_btn.setFixedSize(36, 36)
        self.trash_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.trash_btn.setToolTip(
            self.tr("Trash: deleted files (restore / delete permanently)")
        )
        top_bar_layout.addWidget(self.trash_btn)

        self.status_combo = QComboBox()
        self.status_combo.setObjectName("statusCombo")
        self.status_combo.addItem(self.tr("All"), userData="all")
        self.status_combo.addItem(self.tr("Completed"), userData="complete")
        self.status_combo.addItem(self.tr("In progress"), userData="incomplete")
        self.status_combo.currentIndexChanged.connect(self.reload_items)
        self.status_combo.setMinimumHeight(36)
        self.status_combo.setFixedWidth(154)
        self.status_combo.setToolTip(self.tr("Filter by transfer status"))
        top_bar_layout.addWidget(self.status_combo)

        self.filter_btn = QPushButton(self.tr("Apply"))
        self.filter_btn.setObjectName("applyFilterButton")
        self.filter_btn.setFixedSize(116, 36)
        self.filter_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.filter_btn.setToolTip(self.tr("Apply the current filters"))
        top_bar_layout.addWidget(self.filter_btn)

        top_bar_layout.addSpacing(4)
        self.btn_reconnect = QPushButton("⟳")
        self.btn_reconnect.setObjectName("topActionButton")
        self.btn_reconnect.setFixedSize(36, 36)
        self.btn_reconnect.setEnabled(False)
        self.btn_reconnect.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reconnect.setToolTip(self.tr("Reconnect Telegram"))
        top_bar_layout.addWidget(self.btn_reconnect)

        self.btn_settings = QPushButton("⚙")
        self.btn_settings.setObjectName("topActionButton")
        self.btn_settings.setFixedSize(36, 36)
        self.btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_settings.setToolTip(self.tr("Settings"))
        top_bar_layout.addWidget(self.btn_settings)

        self.btn_accounts = QPushButton("◈")
        self.btn_accounts.setObjectName("topActionButton")
        self.btn_accounts.setFixedSize(36, 36)
        self.btn_accounts.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_accounts.setToolTip(self.tr("Telegram Accounts"))
        top_bar_layout.addWidget(self.btn_accounts)

        self._build_language_switcher(top_bar_layout)
        self._apply_top_bar_icons()

        root.addWidget(top_bar)

        splitter = QSplitter()
        splitter.setObjectName("mainSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)
        root.addWidget(splitter, 1)

        left_panel = QFrame()
        left_panel.setObjectName("leftPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(10)
        left_header = QHBoxLayout()
        left_header.setSpacing(8)
        self._folder_title_label = QLabel(self.tr("Folders"))
        self._folder_title_label.setObjectName("panelTitle")
        left_header.addWidget(self._folder_title_label)
        left_header.addStretch(1)
        self._folder_hint_label = QLabel(self.tr("Structure"))
        self._folder_hint_label.setObjectName("panelHint")
        left_header.addWidget(self._folder_hint_label)
        left_layout.addLayout(left_header)

        self.folder_tree = QTreeView()
        self.folder_tree.setObjectName("folderTree")
        self.folder_tree.setModel(self.folder_model)
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setUniformRowHeights(True)
        self.folder_tree.setAnimated(True)
        self.folder_tree.setRootIsDecorated(False)
        self.folder_tree.setItemsExpandable(True)
        self.folder_tree.setIconSize(QSize(22, 22))
        self.folder_tree.setIndentation(20)
        left_layout.addWidget(self.folder_tree, 1)
        splitter.addWidget(left_panel)

        right_panel = ExplorerDropFrame()
        right_panel.setObjectName("rightPanel")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(14, 14, 14, 14)
        right_layout.setSpacing(10)

        right_header = QHBoxLayout()
        right_header.setSpacing(8)
        self._objects_title_label = QLabel(self.tr("Files in the cloud"))
        self._objects_title_label.setObjectName("panelTitle")
        right_header.addWidget(self._objects_title_label)
        right_header.addStretch(1)
        self._objects_hint_label = QLabel(
            self.tr("Drag files here or right-click for actions")
        )
        self._objects_hint_label.setObjectName("panelHint")
        right_header.addWidget(self._objects_hint_label)
        right_layout.addLayout(right_header)

        self.explorer_view = ExplorerListView()
        self.explorer_view.setObjectName("explorerView")
        self.explorer_view.setModel(self.explorer_model)
        self.explorer_view.setViewMode(QListView.ViewMode.IconMode)
        self.explorer_view.setFlow(QListView.Flow.LeftToRight)
        self.explorer_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.explorer_view.setMovement(QListView.Movement.Static)
        self.explorer_view.setWrapping(True)
        self.explorer_view.setUniformItemSizes(True)
        self.explorer_view.setWordWrap(True)
        self.explorer_view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.explorer_view.setSelectionRectVisible(True)
        self.explorer_view.setSpacing(6)
        icon_sz = getattr(self.config, "ui_icon_size", 56)
        self.explorer_model.set_icon_size(icon_sz)
        self.explorer_view.setGridSize(QSize(icon_sz + 44, icon_sz + 54))
        self.explorer_view.setIconSize(QSize(icon_sz, icon_sz))
        self.explorer_view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.explorer_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.explorer_view.setToolTip(
            self.tr("Double-click to download. Right-click for more actions.")
        )
        self.explorer_view.export_paths_provider = self._provide_export_paths_for_drag
        self.explorer_view.export_success_notifier = self._on_export_success

        explorer_stack_host = QWidget()
        explorer_stack_host.setObjectName("explorerStackHost")
        self._explorer_stack = QStackedLayout(explorer_stack_host)
        self._explorer_stack.setContentsMargins(0, 0, 0, 0)
        self._explorer_stack.addWidget(self.explorer_view)
        self._empty_state_label = QLabel(explorer_stack_host)
        self._empty_state_label.setObjectName("emptyStateLabel")
        self._empty_state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_state_label.setWordWrap(True)
        self._empty_state_label.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._empty_state_label.customContextMenuRequested.connect(
            self._on_empty_state_context_menu
        )
        self._explorer_stack.addWidget(self._empty_state_label)
        right_layout.addWidget(explorer_stack_host, 1)
        self._right_drop_frame = right_panel

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 1080])

        # Global progress bar
        self.progress_widget = ProgressLogWidget(self)
        self.progress_widget.setObjectName("progressWidget")
        root.addWidget(self.progress_widget)

        # Log overlay
        self.progress_widget.logs_container.setParent(central)
        self.progress_widget.logs_container.hide()

        # Bottom status bar toggle
        self.log_toggle_btn = QPushButton()
        self.log_toggle_btn.setObjectName("logToggleButton")
        self.log_toggle_btn.setCheckable(True)
        self.log_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.statusBar().addPermanentWidget(self.log_toggle_btn)

        self.process_toggle_btn = QPushButton()
        self.process_toggle_btn.setObjectName("processToggleButton")
        self.process_toggle_btn.setCheckable(True)
        self.process_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.statusBar().addPermanentWidget(self.process_toggle_btn)

        self._update_toggle_button_labels()
        self.statusBar().showMessage(self.tr("Ready"))
        self._sync_empty_state()

    def _build_language_switcher(self, top_bar_layout: QHBoxLayout) -> None:
        """A one-click language switch button, always visible in the top bar."""
        self.lang_button = QToolButton()
        self.lang_button.setObjectName("topActionButton")
        self.lang_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.lang_button.setFixedHeight(36)
        self.lang_button.setMinimumWidth(72)
        self.lang_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lang_button.setToolTip(self.tr("Change interface language"))

        self._lang_menu = QMenu(self.lang_button)
        for lang in available_languages():
            action = QAction(f"{lang.flag}  {lang.native_name}", self._lang_menu)
            action.triggered.connect(
                lambda _checked=False, code=lang.code: self._switch_language(code)
            )
            self._lang_menu.addAction(action)
        self.lang_button.setMenu(self._lang_menu)
        self._update_language_button_label()
        top_bar_layout.addWidget(self.lang_button)

    def _update_language_button_label(self) -> None:
        current = language_by_code(saved_language())
        self.lang_button.setText(f"{current.flag} {current.code[:2].upper()}")

    def _apply_top_bar_icons(self) -> None:
        if qta is None:
            return

        icon_specs = (
            (self.nav_back_btn, "fa6s.chevron-left", QSize(16, 16)),
            (self.nav_forward_btn, "fa6s.chevron-right", QSize(16, 16)),
            (self.nav_up_btn, "fa6s.house", QSize(16, 16)),
            (self.btn_reconnect, "fa6s.arrows-rotate", QSize(16, 16)),
            (self.btn_settings, "fa6s.sliders", QSize(16, 16)),
            (self.btn_accounts, "fa6s.user-group", QSize(16, 16)),
        )
        for button, icon_name, icon_size in icon_specs:
            button.setText("")
            button.setIcon(
                qta.icon(
                    icon_name,
                    color="#a1a1aa",
                    color_active="#ffffff",
                    color_disabled="#52525b",
                )
            )
            button.setIconSize(icon_size)

    def _wire_events(self) -> None:
        self.action_create_folder.triggered.connect(lambda: self._on_create_folder())
        self.action_upload.triggered.connect(self._on_upload)
        self.action_download.triggered.connect(lambda: self._on_download())
        self.action_download_folder.triggered.connect(
            lambda: self._on_download_folder()
        )
        self.action_delete_local.triggered.connect(self._on_delete_local)
        self.action_delete.triggered.connect(self._on_delete_remote)
        self.action_refresh.triggered.connect(lambda: self._enqueue_refresh(full=False))
        self.action_reconcile.triggered.connect(self._enqueue_reconcile)
        self.action_reindex.triggered.connect(lambda: self._enqueue_refresh(full=True))
        self.action_settings.triggered.connect(self._on_settings)
        self.action_reconnect.triggered.connect(self._on_reconnect)
        self.action_accounts.triggered.connect(self._on_accounts)
        self.btn_settings.clicked.connect(self._on_settings)
        self.btn_reconnect.clicked.connect(self._on_reconnect)
        self.btn_accounts.clicked.connect(self._on_accounts)

        self.nav_back_btn.clicked.connect(self._on_nav_back)
        self.nav_forward_btn.clicked.connect(self._on_nav_forward)
        self.nav_up_btn.clicked.connect(self._on_nav_up)

        self.filter_btn.clicked.connect(self.reload_items)
        self.search_edit.returnPressed.connect(self.reload_items)
        self.search_edit.textChanged.connect(
            lambda _: self._search_debounce_timer.start()
        )
        self.status_combo.currentIndexChanged.connect(lambda _: self.reload_items())
        self.search_everywhere_btn.toggled.connect(lambda _: self.reload_items())
        self.trash_btn.toggled.connect(self._on_toggle_trash_view)

        self.folder_tree.clicked.connect(self._on_folder_clicked)
        self.folder_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.folder_tree.customContextMenuRequested.connect(
            self._on_folder_context_menu
        )

        self.explorer_view.clicked.connect(lambda _: self._refresh_action_state())
        self.explorer_view.selectionModel().selectionChanged.connect(
            lambda *_: self._refresh_action_state()
        )
        self.explorer_view.doubleClicked.connect(self._on_item_activated)
        self.explorer_view.files_dropped.connect(self._on_files_dropped)

        self.log_toggle_btn.clicked.connect(self._toggle_logs)
        self.process_toggle_btn.clicked.connect(self._toggle_processes)

        self.explorer_view.drag_state_changed.connect(
            self._right_drop_frame._set_drop_active
        )
        self.explorer_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.explorer_view.customContextMenuRequested.connect(
            self._on_explorer_context_menu
        )
        self._right_drop_frame.files_dropped.connect(self._on_files_dropped)
        self.progress_widget.cancel_requested.connect(self._on_cancel_job)

        self._configure_shortcuts()
        self._refresh_action_state()

    def _update_toggle_button_labels(self) -> None:
        self.log_toggle_btn.setText(
            ("▼ " if self.log_toggle_btn.isChecked() else "▲ ") + self.tr("Logs")
        )
        self.process_toggle_btn.setText(
            ("▼ " if self.process_toggle_btn.isChecked() else "▲ ")
            + self.tr("Processes")
        )

    def _toggle_logs(self, checked: bool = False) -> None:
        if self.log_toggle_btn.isChecked():
            self.progress_widget.logs_container.show()
            self.progress_widget.logs_container.raise_()
        else:
            self.progress_widget.logs_container.hide()
        self._update_toggle_button_labels()

    def _toggle_processes(self, checked: bool = False) -> None:
        if self.process_toggle_btn.isChecked():
            self._toast_overlay.show()
            self._toast_overlay.raise_()
        else:
            self._toast_overlay.hide()
        self._update_toggle_button_labels()

    def _cleanup_thumbnail_dirs_async(self) -> None:
        """On startup: purge stale temp images (.thumb_fetch), cap the thumbnail
        disk cache (.thumb_cache) with LRU eviction, and drop the ephemeral cache
        of files assembled for share links (.share_cache — rebuilt on demand).
        Runs in the background."""
        cache_root = Path(self.config.cache_dir).expanduser()
        fetch_dir = self._thumb_fetch_dir
        thumb_cache = str(cache_root / ".thumb_cache")
        share_cache = str(cache_root / ".share_cache")

        def _run() -> None:
            import shutil

            from app.core.utils import clear_dir_files, evict_dir_to_limit

            try:
                clear_dir_files(fetch_dir)
                evict_dir_to_limit(thumb_cache, max_files=3000)
                shutil.rmtree(share_cache, ignore_errors=True)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _run_stream_cleanup(self) -> None:
        """Periodic LRU cleanup of the streaming cache while media is being viewed."""
        max_mb = int(getattr(self.config, "stream_cache_max_mb", 2048))
        if max_mb <= 0:
            return  # 0 = no limit, never evict
        cache_root = Path(self.config.cache_dir).expanduser()
        stream_cache = cache_root / ".share_cache" / ".stream"
        if not stream_cache.exists():
            return

        def _run() -> None:
            from app.core.cache import CacheManager

            try:
                CacheManager().cleanup(stream_cache, max_bytes=max_mb * 1024 * 1024)
            except Exception as e:
                import logging

                logging.getLogger(__name__).debug("Stream cleanup failed: %s", e)

        threading.Thread(target=_run, daemon=True).start()

    def reload_all(self) -> None:
        self._all_folders = [entry.folder_path for entry in self.repo.list_folders()]
        self.folder_model.set_folders(self._all_folders)

        if self.current_folder:
            existing = any(
                folder == self.current_folder
                or folder.startswith(f"{self.current_folder}/")
                for folder in self._all_folders
            )
            if not existing:
                self.current_folder = None

        self._sync_folder_selection()
        self.reload_items()

    def reload_items(self) -> None:
        from app.core.utils import build_safe_output_path
        from app.ui.models_qt import ExplorerFolderItem, ExplorerFileItem

        if self._trash_view:
            self._reload_trash_items()
            return

        search = self.search_edit.text().strip() or None
        # Compare against the combo box's item data, not its display text —
        # display text is translated and changes with the active language.
        status_key = self.status_combo.currentData()
        status_filter = None if status_key in (None, "all") else str(status_key)

        # "Everywhere" + a non-empty query → recursive search across the whole
        # subtree (or the whole cloud if we're at the root). Child folders are
        # not shown in this mode — the result is a flat list of files from
        # different folders.
        recursive_search = bool(self.search_everywhere_btn.isChecked()) and bool(search)

        items: list = []
        if not recursive_search:
            for child in self._list_child_folders(self.current_folder):
                items.append(
                    ExplorerFolderItem(name=child.rsplit("/", 1)[-1], path=child)
                )

        file_rows: list = []
        if recursive_search:
            file_rows = self.repo.list_objects_unified(
                folder_path=self.current_folder,
                search=search,
                status=status_filter,
                recursive=True,
            )
        elif self.current_folder:
            file_rows = self.repo.list_objects_by_folder(
                self.current_folder,
                search=search,
                status=status_filter,
            )

        eager_local_presence = len(file_rows) <= self._EAGER_LOCAL_PRESENCE_LIMIT
        self._lazy_presence_mode = not eager_local_presence

        # Cheap per-folder aggregates for the damaged/offline overlay + notes.
        from app.core.object_state import display_state as _display_state

        chat_ids_map: dict = {}
        lost_keys: set = set()
        notes_map: dict = {}
        # In a recursive search, results span multiple folders, so per-folder
        # aggregates don't apply — fall back to the stored status (no overlay).
        if self.current_folder and file_rows and not recursive_search:
            try:
                chat_ids_map = self.repo.get_part_chat_ids_by_folder(
                    self.current_folder
                )
                lost_keys = self.repo.get_lost_file_keys_by_folder(self.current_folder)
                notes_map = self.repo.get_object_notes_by_folder(self.current_folder)
            except Exception:
                chat_ids_map, lost_keys, notes_map = {}, set(), {}
        connected_ids = self._connected_chat_ids()

        for row in file_rows:
            local_path = None
            local_exists = False
            try:
                local = build_safe_output_path(
                    self.config.download_root, row.folder_path, row.orig_name
                )
                local_path = str(local)
                if eager_local_presence:
                    local_exists = self._check_local_exists_cached(local)
            except Exception:
                local_path = None
                local_exists = False
            computed_state = _display_state(
                stored_status=row.status,
                part_chat_ids=chat_ids_map.get(row.file_key, set()),
                has_lost_part=row.file_key in lost_keys,
                connected_chat_ids=connected_ids,
            )
            items.append(
                ExplorerFileItem(
                    entry=row,
                    local_path=local_path,
                    local_exists=local_exists,
                    display_state=computed_state,
                    note=notes_map.get(row.file_key, ""),
                )
            )

        self.explorer_model.set_items(items)
        self._sync_loading_badge_timer()
        self._sync_empty_state()
        if self._lazy_presence_mode and file_rows:
            QTimer.singleShot(0, self._refresh_visible_local_presence)

        self.explorer_view.clearSelection()
        self.explorer_view.setCurrentIndex(QModelIndex())
        selection_model = self.explorer_view.selectionModel()
        if selection_model is not None:
            selection_model.clearCurrentIndex()

        self._update_path_bar()
        self.statusBar().showMessage(self.tr("Items: {0}").format(len(items)))
        self._refresh_action_state()

    def _on_toggle_trash_view(self, checked: bool) -> None:
        self._trash_view = bool(checked)
        # Search/folders don't apply in Trash — disable those controls for clarity.
        self.search_edit.setEnabled(not self._trash_view)
        self.search_everywhere_btn.setEnabled(not self._trash_view)
        self.reload_items()

    def _reload_trash_items(self) -> None:
        from app.core.utils import build_safe_output_path
        from app.ui.models_qt import ExplorerFileItem

        try:
            rows = self.repo.list_trash()
        except Exception:
            rows = []

        items: list = []
        for row in rows:
            local_path = None
            local_exists = False
            try:
                local = build_safe_output_path(
                    self.config.download_root, row.folder_path, row.orig_name
                )
                local_path = str(local)
                local_exists = self._check_local_exists_cached(local)
            except Exception:
                local_path, local_exists = None, False
            items.append(
                ExplorerFileItem(
                    entry=row,
                    local_path=local_path,
                    local_exists=local_exists,
                    display_state=row.status,
                    note="",
                )
            )

        self.explorer_model.set_items(items)
        self._sync_loading_badge_timer()
        self._sync_empty_state()
        self.explorer_view.clearSelection()
        self.explorer_view.setCurrentIndex(QModelIndex())
        self._update_path_bar()
        self.statusBar().showMessage(self.tr("Trash: {0}").format(len(items)))
        self._refresh_action_state()

    def _sync_empty_state(self) -> None:
        if not hasattr(self, "_explorer_stack"):
            return
        has_items = self.explorer_model.rowCount() > 0
        if has_items:
            self._explorer_stack.setCurrentWidget(self.explorer_view)
            return

        if self._trash_view:
            self._empty_state_label.setText(
                self.tr("Trash is empty.\nDeleted files land here and can be restored.")
            )
        elif self.current_folder:
            self._empty_state_label.setText(
                self.tr(
                    "This folder is empty.\nDrag files here, click Upload, or "
                    "create a subfolder."
                )
            )
        else:
            self._empty_state_label.setText(
                self.tr(
                    "No folder selected.\nPick a folder on the left or create a new one."
                )
            )
        self._explorer_stack.setCurrentWidget(self._empty_state_label)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Esc on the main window opens the "Quit or minimize" dialog.
        # Only fires if the focused child widget/dialog didn't consume Esc first.
        if event.key() == Qt.Key.Key_Escape and not getattr(
            self, "_shutdown_started", False
        ):
            event.accept()
            self.close()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.LanguageChange:
            self._retranslate_ui()
        super().changeEvent(event)

    def _retranslate_ui(self) -> None:
        """Re-apply every translatable string after a language switch.

        Qt does not retranslate widget text automatically when the active
        translator changes — each piece of text has to be re-set via tr().
        """
        self.action_settings.setText(self.tr("Settings"))
        self.action_reconnect.setText(self.tr("Reconnect"))
        self.action_accounts.setText(self.tr("Telegram Accounts"))
        self.action_create_folder.setText(self.tr("Create folder"))
        self.action_upload.setText(self.tr("Upload"))
        self.action_download.setText(self.tr("Download"))
        self.action_download_folder.setText(self.tr("Download folder"))
        self.action_delete_local.setText(self.tr("Delete locally"))
        self.action_delete.setText(self.tr("Delete remotely"))
        self.action_refresh.setText(self.tr("Refresh"))
        self.action_reconcile.setText(self.tr("Reconcile database"))
        self.action_reindex.setText(self.tr("Full reindex"))

        self.nav_back_btn.setToolTip(self.tr("Back (Alt+Left)"))
        self.nav_forward_btn.setToolTip(self.tr("Forward (Alt+Right)"))
        self.nav_up_btn.setToolTip(self.tr("Up (Alt+Up)"))
        self.path_bar.setPlaceholderText(self.tr("Cloud path"))
        self.path_bar.setToolTip(self.tr("Current path"))
        self.search_edit.setPlaceholderText(self.tr("Search by file name"))
        self.search_edit.setToolTip(
            self.tr("Search files in the current folder (Ctrl+F)")
        )
        self.search_everywhere_btn.setText(self.tr("Everywhere"))
        self.search_everywhere_btn.setToolTip(
            self.tr("Search all nested folders (recursively), not just the current one")
        )
        self.trash_btn.setToolTip(
            self.tr("Trash: deleted files (restore / delete permanently)")
        )
        self.filter_btn.setText(self.tr("Apply"))
        self.filter_btn.setToolTip(self.tr("Apply the current filters"))
        self.btn_reconnect.setToolTip(self.tr("Reconnect Telegram"))
        self.btn_settings.setToolTip(self.tr("Settings"))
        self.btn_accounts.setToolTip(self.tr("Telegram Accounts"))
        self.lang_button.setToolTip(self.tr("Change interface language"))
        self._update_language_button_label()

        idx = self.status_combo.currentIndex()
        self.status_combo.setItemText(0, self.tr("All"))
        self.status_combo.setItemText(1, self.tr("Completed"))
        self.status_combo.setItemText(2, self.tr("In progress"))
        self.status_combo.setCurrentIndex(idx)

        self._folder_title_label.setText(self.tr("Folders"))
        self._folder_hint_label.setText(self.tr("Structure"))
        self._objects_title_label.setText(self.tr("Files in the cloud"))
        self._objects_hint_label.setText(
            self.tr("Drag files here or right-click for actions")
        )
        self.explorer_view.setToolTip(
            self.tr("Double-click to download. Right-click for more actions.")
        )

        self._update_toggle_button_labels()
        self._tray_action_show.setText(self.tr("Show"))
        self._tray_action_quit.setText(self.tr("Quit"))
        self._sync_empty_state()

    def closeEvent(self, event) -> None:  # noqa: N802
        # If there are active jobs, warn user
        has_active = bool(self._active_jobs or self._pending_upload_jobs)
        if has_active:
            box = QMessageBox(self)
            box.setWindowTitle(self.tr("Active transfers"))
            box.setText(
                self.tr(
                    "You have {active} active job(s) and {pending} waiting.\n"
                    "They will be cancelled on exit."
                ).format(
                    active=len(self._active_jobs),
                    pending=len(self._pending_upload_jobs),
                )
            )
            box.setInformativeText(self.tr("What would you like to do?"))
            minimize_btn = box.addButton(
                self.tr("Minimize to tray"), QMessageBox.ButtonRole.AcceptRole
            )
            wait_btn = box.addButton(
                self.tr("Wait for completion"), QMessageBox.ButtonRole.ActionRole
            )
            box.addButton(self.tr("Quit now"), QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(minimize_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked == minimize_btn:
                event.ignore()
                self.hide()
                return
            if clicked == wait_btn:
                event.ignore()
                self.progress_widget.append_log(
                    "Waiting for active transfers to complete. You can cancel manually."
                )
                return
            # quit_btn → proceed with shutdown below
        else:
            from app.ui.dialogs import ConfirmDialog

            dialog = ConfirmDialog(
                title=self.tr("Close"),
                message=self.tr("Quit the application or minimize to tray?"),
                parent=self,
                is_destructive=False,
            )
            # Enter (default button) = quit, Esc = minimize to tray.
            dialog.btn_confirm.setText(self.tr("Quit"))
            dialog.btn_cancel.setText(self.tr("Minimize"))
            result = dialog.exec()
            if result != QDialog.DialogCode.Accepted:
                # Esc / "Minimize" / closing the dialog itself → hide to tray.
                event.ignore()
                self.hide()
                return
            # Accepted → proceed with the normal shutdown below.

        # Graceful shutdown: stop worker → disconnect → close DB
        self._shutdown_started = True
        self.progress_widget.append_log("Shutting down... cancelling active jobs")
        self.statusBar().showMessage(self.tr("Shutting down…"))
        self._toast_overlay.hide_all()
        self._tray.hide()

        # Cancel all jobs and stop worker
        for job_id in list(self._active_jobs):
            self.worker.cancel_job(job_id)
        self.worker.request_stop()
        self.worker.wait(15_000)  # wait up to 15s for clean shutdown

        event.accept()

    def _switch_language(self, lang_code: str) -> None:
        save_language(lang_code)
        app = QApplication.instance()
        if hasattr(app, "tg_translator"):
            install_language(app, lang_code, app.tg_translator)
        self._retranslate_ui()
