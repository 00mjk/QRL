#!/usr/bin/env python3
# coding=utf-8
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.

import sys
if sys.version_info < (3, 5):
    print("This application requires at least Python 3.5")
    quit(1)

from qrl.core.DependencyChecker import DependencyChecker

DependencyChecker.check()

from qrl.main import main

main()
