#!/usr/bin/env python
# License: GPLv3 Copyright: 2023, Kovid Goyal <kovid at kovidgoyal.net>


import os
import posixpath
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from qt.core import (
    QAbstractItemView, QAbstractListModel, QComboBox, QDialogButtonBox, QHBoxLayout,
    QIcon, QItemSelection, QItemSelectionModel, QLabel, QListView, QPushButton, QSize,
    QStyledItemDelegate, Qt, QTimer, QVBoxLayout, pyqtSignal, sip,
)

from calibre import human_readable
from calibre.db.constants import DATA_DIR_NAME, DATA_FILE_PATTERN
from calibre.gui2 import (
    choose_files, error_dialog, file_icon_provider, gprefs, question_dialog, open_local_file
)
from calibre.gui2.dialogs.confirm_delete import confirm
from calibre.gui2.widgets2 import Dialog
from calibre.utils.icu import primary_sort_key
from calibre.utils.recycle_bin import delete_file
from calibre_extensions.progress_indicator import set_no_activate_on_click

NAME_ROLE = Qt.ItemDataRole.UserRole


class Delegate(QStyledItemDelegate):

    rename_requested = pyqtSignal(int, str)

    def setModelData(self, editor, model, index):
        newname = editor.text()
        oldname = index.data(NAME_ROLE) or ''
        if newname != oldname:
            self.rename_requested.emit(index.row(), newname)

    def setEditorData(self, editor, index):
        name = index.data(NAME_ROLE) or ''
        # We do this because Qt calls selectAll() unconditionally on the
        # editor, and we want only a part of the file name to be selected
        QTimer.singleShot(0, partial(self.set_editor_data, name, editor))

    def set_editor_data(self, name, editor):
        if sip.isdeleted(editor):
            return
        editor.setText(name)
        ext_pos = name.rfind('.')
        slash_pos = name.rfind(os.sep)
        if slash_pos == -1 and ext_pos > 0:
            editor.setSelection(0, ext_pos)
        elif ext_pos > -1 and slash_pos > -1 and ext_pos > slash_pos + 1:
            editor.setSelection(slash_pos+1, ext_pos - slash_pos - 1)
        else:
            editor.selectAll()


class Files(QAbstractListModel):

    def __init__(self, db, book_id, parent=None):
        self.db = db
        self.book_id = book_id
        super().__init__(parent=parent)
        self.fi = file_icon_provider()
        self.files = []

    def refresh(self, key=None, reverse=False):
        self.modelAboutToBeReset.emit()
        self.files = sorted(self.db.list_extra_files(self.book_id, pattern=DATA_FILE_PATTERN), key=key or self.file_sort_key, reverse=reverse)
        self.modelReset.emit()

    def file_sort_key(self, ef):
        return primary_sort_key(ef.relpath)

    def date_sort_key(self, ef):
        return ef.stat_result.st_mtime

    def size_sort_key(self, ef):
        return ef.stat_result.st_size

    def resort(self, which):
        k, reverse = self.file_sort_key, False
        if which == 1:
            k, reverse = self.date_sort_key, True
        elif which == 2:
            k, reverse = self.size_sort_key, True
        self.refresh(key=k, reverse=reverse)

    def rowCount(self, parent=None):
        return len(self.files)

    def file_display_name(self, rownum):
        ef = self.files[rownum]
        name = ef.relpath.split('/', 1)[1]
        return name.replace('/', os.sep)

    def item_at(self, rownum):
        return self.files[rownum]

    def rownum_for_relpath(self, relpath):
        for i, e in enumerate(self.files):
            if e.relpath == relpath:
                return i
        return -1

    def data(self, index, role):
        row = index.row()
        if row >= len(self.files):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            name =  self.file_display_name(row)
            e = self.item_at(row)
            date = datetime.fromtimestamp(e.stat_result.st_mtime)
            l2 = human_readable(e.stat_result.st_size) + date.strftime(' [%Y/%m/%d]')
            return name + '\n' + l2
        if role == Qt.ItemDataRole.DecorationRole:
            ef = self.files[row]
            fmt = ef.relpath.rpartition('.')[-1].lower()
            return self.fi.icon_from_ext(fmt)
        if role == NAME_ROLE:
            return self.file_display_name(row)
        return None

    def flags(self, index):
        return Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable


class DataFilesManager(Dialog):

    def __init__(self, db, book_id, parent=None):
        self.db = db.new_api
        self.book_title = title = self.db.field_for('title', book_id) or _('Unknown')
        self.book_id = book_id
        super().__init__(_('Manage data files for {}').format(title), 'manage-data-files-xx',
                         parent=parent, default_buttons=QDialogButtonBox.StandardButton.Close)

    def sizeHint(self):
        return QSize(400, 500)

    def setup_ui(self):
        self.l = l = QVBoxLayout(self)

        self.sbla = la = QLabel(_('&Sort by:'))
        self.sort_by = sb = QComboBox(self)
        sb.addItems((_('Name'), _('Recency'), _('Size')))
        sb.setCurrentIndex(gprefs.get('manage_data_files_last_sort_idx', 0))
        sb.currentIndexChanged.connect(self.sort_changed)
        sb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        la.setBuddy(sb)
        h = QHBoxLayout()
        l.addLayout(h)
        h.addWidget(la), h.addWidget(sb)

        self.delegate = d = Delegate(self)
        d.rename_requested.connect(self.rename_requested, type=Qt.ConnectionType.QueuedConnection)
        self.fview = v = QListView(self)
        set_no_activate_on_click(v)
        v.activated.connect(self.activated)
        v.setItemDelegate(d)
        l.addWidget(v)
        self.files = Files(self.db.new_api, self.book_id, parent=v)
        self.files.resort(self.sort_by.currentIndex())
        v.setModel(self.files)
        v.setEditTriggers(QAbstractItemView.EditTrigger.AnyKeyPressed | QAbstractItemView.EditTrigger.EditKeyPressed)
        v.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        if self.files.rowCount():
            v.setCurrentIndex(self.files.index(0))
        v.selectionModel().currentChanged.connect(self.current_changed)

        self.current_label = la = QLabel(self)
        la.setWordWrap(True)
        l.addWidget(la)

        l.addWidget(self.bb)
        self.add_button = b = QPushButton(QIcon.ic('plus.png'), _('&Add files'), self)
        b.clicked.connect(self.add_files)
        self.bb.addButton(b, QDialogButtonBox.ButtonRole.ActionRole)
        self.remove_button = b = QPushButton(QIcon.ic('minus.png'), _('&Remove files'), self)
        b.clicked.connect(self.remove_files)
        self.bb.addButton(b, QDialogButtonBox.ButtonRole.ActionRole)

        self.current_changed()
        self.resize(self.sizeHint())
        self.fview.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Return:
            return
        return super().keyPressEvent(ev)

    def sort_changed(self):
        idx = max(0, self.sort_by.currentIndex())
        gprefs.set('manage_data_files_last_sort_idx', idx)
        with self.preserve_state():
            self.files.resort(idx)

    def current_changed(self):
        idx = self.fview.currentIndex()
        txt = ''
        if idx.isValid():
            txt = self.files.file_display_name(idx.row())
        self.current_label.setText(txt)

    @property
    def current_item(self):
        ci = self.fview.currentIndex()
        try:
            return self.files.item_at(ci.row())
        except Exception:
            return None

    @contextmanager
    def preserve_state(self):
        selected = set()
        vs = self.fview.verticalScrollBar()
        pos = vs.value()
        for idx in self.fview.selectionModel().selectedRows():
            e = self.files.item_at(idx.row())
            selected.add(e.relpath)
        current = self.current_item
        try:
            yield
        finally:
            sm = self.fview.selectionModel()
            sm.clearSelection()
            current_idx = None
            s = QItemSelection()
            for i in range(self.files.rowCount()):
                e = self.files.item_at(i)
                if current is not None and e.relpath == current.relpath:
                    current_idx = i
                if e.relpath in selected:
                    ii = self.files.index(i)
                    s.select(ii, ii)
            if current_idx is not None:
                flags = QItemSelectionModel.SelectionFlag.Current
                if current.relpath in selected:
                    flags |= QItemSelectionModel.SelectionFlag.Select
                sm.setCurrentIndex(self.files.index(current_idx), flags)
            sm.select(s, QItemSelectionModel.SelectionFlag.SelectCurrent)
            self.current_changed()
            vs.setValue(pos)

    def add_files(self):
        files = choose_files(self, 'choose-data-files-to-add', _('Choose files to add'))
        if not files:
            return
        q = self.db.are_paths_inside_book_dir(self.book_id, files, DATA_DIR_NAME)
        if q:
            return error_dialog(
                self, _('Cannot add'), _(
                    'Cannot add these data files to the book because they are already in the book\'s data files folder'
                ), show=True, det_msg='\n'.join(q))

        m = {f'{DATA_DIR_NAME}/{os.path.basename(x)}': x for x in files}
        added = self.db.add_extra_files(self.book_id, m, replace=False, auto_rename=False)
        collisions = set(m) - set(added)
        if collisions:
            if question_dialog(self, _('Replace existing files?'), _(
                    'The following files already exist as data files in the book. Replace them?'
            ) + '\n' + '\n'.join(x.partition('/')[2] for x in collisions)):
                self.db.add_extra_files(self.book_id, m, replace=True, auto_rename=False)
        with self.preserve_state():
            self.files.refresh()

    def remove_files(self):
        files = []
        for idx in self.fview.selectionModel().selectedRows():
            files.append(self.files.item_at(idx.row()))
        if not files:
            return error_dialog(self, _('Cannot delete'), _('No files selected to remove'), show=True)
        if len(files) == 1:
            msg = _('Send the file "{}" to the Recycle Bin?').format(files[0].relpath.replace('/', os.sep))
        else:
            msg = _('Send the {} selected files to the Recycle Bin?').format(len(files))
        if not confirm(msg, 'manage-data-files-confirm-delete'):
            return
        for f in files:
            delete_file(f.file_path, permanent=False)
        with self.preserve_state():
            self.files.refresh()

    def rename_requested(self, idx, new_name):
        e = self.files.item_at(idx)
        newrelpath = posixpath.normpath(posixpath.join(DATA_DIR_NAME, new_name.replace(os.sep, '/')))
        if not newrelpath.startswith(DATA_DIR_NAME + '/'):
            return error_dialog(self, _('Invalid name'), _('"{}" is not a valid file name').format(new_name), show=True)
        if e.relpath not in self.db.rename_extra_files(self.book_id, {e.relpath: newrelpath}, replace=False):
            if question_dialog(self, _('Replace existing file?'), _(
                    'Another data file with the name "{}" already exists. Replace it?').format(new_name)):
                self.db.rename_extra_files(self.book_id, {e.relpath: newrelpath}, replace=True)
        with self.preserve_state():
            self.files.refresh()
        row = self.files.rownum_for_relpath(newrelpath)
        if row > -1:
            idx = self.files.index(row)
            self.fview.setCurrentIndex(idx)
            self.fview.selectionModel().select(idx, QItemSelectionModel.SelectionFlag.SelectCurrent)
            self.fview.scrollTo(idx)

    def activated(self, idx):
        e = self.files.item_at(idx.row())
        open_local_file(e.file_path)


if __name__ == '__main__':
    from calibre.gui2 import Application
    from calibre.library import db as di
    app = Application([])
    dfm = DataFilesManager(di(os.path.expanduser('~/test library')), 1893)
    dfm.exec()
