#!/bin/env dls-python

author = "Tom Cobb"
usage = """%prog [<module_path>]

<module_path> is the path to the module root or configure/RELEASE file. If it
isn't specified, then the current working directory is taken as the module root.
This program is a graphical diplay tool for the configure/RELEASE tree. It 
displays the current tree in the left pane, an updated tree with all modules at
their latest versions in the right pane, and the latest consistent set of
modules in the middle pane. The user then has the chance to change module 
versions between the original and latest numbers, view SVN logs, and edit 
configure/RELEASE files directly. The updated trees can then be written to 
configure/RELEASE, or the changes printed on the commandline."""

import os
import signal
import sys
import traceback
from argparse import ArgumentParser
from subprocess import PIPE, Popen

from PyQt5 import uic
from PyQt5.QtCore import QProcess, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QPalette
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    )

from .tree import dependency_tree
from .tree_update import dependency_tree_update

UI_FILE = os.path.join(os.path.dirname(__file__), 'dependency_checker.ui')

if __name__ == "__main__":
    sys.path.append(os.path.abspath(os.path.join(
        os.path.realpath(__file__), '..', '..', '..', 'dls_environment')))


def build_gui_tree(list_view,tree,parent=None):
    """Function that takes a ListView or ListViewItem, and populates its
    children from a dependency_tree"""
    if parent==None:
        list_view.clear()
    if parent:
        child = QTreeWidgetItem(parent)
    else:
        child = QTreeWidgetItem(list_view)
        list_view.child = child
    child.setText(0, "%s: %s" % (tree.name, tree.version))
    child.tree = tree
    fg = QBrush(Qt.black)
    bg = QBrush(QColor(212,216,236)) # normal - blue
    open_parents = False
    if len(tree.updates())>1:
        bg = QBrush(QColor(203,255,197)) # update available - green
        open_parents = True
    if tree.name in list(list_view.clashes.keys()):
        open_parents = True
        if tree.path == tree.e.sortReleases([x.path for x in \
                                             list_view.clashes[tree.name]])[-1]:
            fg = QBrush(QColor(153,150,0)) # involved in clash: yellow
        else:
            fg = QBrush(Qt.red) # causes clash: red
    if tree.version == "invalid":
        open_parents = True
        fg = QBrush(QColor(160,32,240)) # invalid: purple
    child.setForeground(0, fg)
    child.setBackground(0, bg)
    if open_parents: 
        temp_ob = child
        while temp_ob.parent():
            temp_ob.parent().setExpanded(True)
            temp_ob = temp_ob.parent()
    for leaf in tree.leaves:
        build_gui_tree(list_view,leaf,child)
    if parent==None:
        child.setExpanded(True)

class TreeView(QTreeWidget):
    """Custom tree view widget"""
    def __init__(self,tree,tree_type,*args):
        """Initialise the class.
        tree = dependency_tree to initialise from
        tree_type = string "original","consistent" or "latest" """
        QTreeWidget.__init__(self,*args)
        self.setHeaderLabel("%s Tree"%(tree_type.title()))
        palette = self.viewport().palette()
        palette.setColor(QPalette.Base, QColor(212,216,236))
        self.viewport().setPalette(palette)
        self.tree = tree
        self.clashes = tree.clashes(print_warnings=False)
        self.setRootIsDecorated(True)
        # connect event handlers
        self.viewportEntered.connect(self.mouseout)
        build_gui_tree(self,tree)
        self.child.setExpanded(True)
        self.itemEntered.connect(self.mousein)
        self.setMouseTracking(True)

    def contextMenuEvent(self, event):
        """Popup a context menu at pos, fill it with svn log and revert commands
        depending on the module it is and what version it's at"""
        pos = event.globalPos()
        item = self.itemAt(event.pos())
        if item:
            menu = QMenu()
            self.contextItem = item
            menu.addAction("Edit RELEASE", self.externalEdit)
            if hasattr(item.tree,"versions"):
                if item.tree.version!=item.tree.versions[0][0]:
                    menu.addAction("SVN log", self.svn_log)
                self.context_methods = []
                for version,path in [(v,p) for v,p in item.tree.versions \
                                     if v!=item.tree.version]:
                    self.context_methods.append(reverter(item.tree,self,path))
                    menu.addAction("Change to %s"%version, \
                                    self.context_methods[-1].revert)
            menu.exec_(pos)

    def svn_log(self):
        """Do a dls-logs-since-release.py to find out the svn logs between the
        original release number and the current release number."""
        leaf = self.contextItem.tree
        args = ["dls-logs-since-release.py","-r",leaf.name]
        if leaf.versions[0][0] != "work":
            args += [leaf.versions[0][0],leaf.version]
        p = Popen(args, stdout = PIPE, stderr = PIPE)
        (stdout, stderr) = p.communicate()
        text = stdout.strip()
        x = formLog(text,self)
        x.setWindowTitle("SVN Log: %s"%leaf.name)
        x.show()

    def externalEdit(self):
        """Open the configure/RELEASE in gedit"""
        item = self.contextItem
        if item and os.path.isfile(item.tree.release()):
            proc = QProcess(self)
            proc.start("gedit",[item.tree.release()])

    def mouseout(self):
        """Show hints in the statusBar on mouseout"""
        self.top.statusBar.showMessage("----- Hover over a module for its path, "\
                                     "right click for a context menu -----")

    def mousein(self, item, col):
        """Show item path in the statusBar on mousein"""
        text = "%s - current: %s" %(item.tree.name, item.tree.path)
        updates = item.tree.updates()
        if len(updates)>1:
            text += ", latest: %s" % updates[-1]
        self.top.statusBar.showMessage(text)

    def confirmWrite(self):
        """Popup a confimation box for writing changes to RELEASE"""
        response=QMessageBox.question(None,"Write Changes",\
             "Would you like to write your changes to configure/RELEASE?",\
             QMessageBox.Yes,QMessageBox.No)
        if response == QMessageBox.Yes:
            self.update.write_changes()

    def printChanges(self):
        text = self.update.print_changes()
        x = formLog(text,self)
        x.setWindowTitle("RELEASE Changes")
        x.show()

class reverter:
    """One shot class to revert a leaf in a list view to path"""
    def __init__(self,leaf,list_view,path):
        """leaf = dependency_tree node to revert
        path = new path to revert to
        list_view = ListViewItem associated with leaf"""
        self.leaf = leaf
        self.list_view = list_view
        self.path = path

    def revert(self):
        """Do the revert"""
        new_leaf = dependency_tree(self.leaf.parent,self.path)
        new_leaf.versions = self.leaf.versions
        self.list_view.tree.replace_leaf(self.leaf,new_leaf)
        self.list_view.clashes=self.list_view.tree.clashes(print_warnings=False)
        build_gui_tree(self.list_view,self.list_view.tree)

class formLog(QDialog):
    """SVN log form"""
    def __init__(self,text,*args):
        """text = text to display in a readonly QTextEdit"""
        QDialog.__init__(self,*args)
        formLayout = QGridLayout(self)#,1,1,11,6,"formLayout")
        self.scroll = QScrollArea(self)
        self.lab = QTextEdit()
        self.lab.setFont(QFont('monospace', 10))
        self.lab.setText(text)
        self.lab.setReadOnly(True)
        self.scroll.setWidget(self.lab)
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumWidth(700)
        formLayout.addWidget(self.scroll,1,1)
        self.btnClose = QPushButton("btnClose", self)
        formLayout.addWidget(self.btnClose,2,1)
        self.btnClose.clicked.connect(self.close)
        self.btnClose.setText("Close")

def dependency_checker():
    """Parses arguments, intialises treeviews and displays them"""
    parser = ArgumentParser(description=usage)
    parser.add_argument("module_path", nargs='?', default=os.getcwd(), type=str, help="Path to RELEASE file")
    args = parser.parse_args()
    path = os.path.abspath(args.module_path)

    app = QApplication([])
    window = QMainWindow()
    top = uic.loadUi(UI_FILE, window)
    top.statusBar = window.statusBar()
    tree = dependency_tree(None,path)
    window.setWindowTitle("Tree Browser - %s: %s, Epics: %s" % (
                            tree.name, tree.version, tree.e.epicsVer()))

    for loc in ["original","latest","consistent"]:
        def displayMessage(message):
            getattr(top,loc+"Write").setEnabled(False)
            getattr(top,loc+"Print").setEnabled(False)
            label = QTextEdit(getattr(top,loc+"Frame"))
            label.setReadOnly(True)
            label.setText(loc.title() + " Updated Tree:\n\n" + message)
            return label
        grid = QGridLayout()
        try:
            update = dependency_tree_update(tree,consistent=(loc=="consistent"),update=(loc!="original"))
            if loc=="original" or not update.new_tree == tree:
                view = TreeView(update.new_tree,loc,getattr(top,loc+"Frame"))
                view.top = top
                view.update = update
                getattr(top,loc+"Write").clicked.connect(view.confirmWrite)
                getattr(top,loc+"Print").clicked.connect(view.printChanges)
                grid.addWidget(view)
            else:
                grid.addWidget(displayMessage("Updated tree is identical to Original tree"))

        except:
            grid.addWidget(displayMessage("Error in tree update...\n\n"+traceback.format_exc()))
        getattr(top,loc+"Frame").setLayout(grid)

    window.show()
    # catch CTRL-C
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app.exec_()

if __name__ == "__main__":
    dependency_checker()
