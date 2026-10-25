# YomkServerPy source distribution

- Repository: https://github.com/Solitude-5309/YomkServer
- Branch: python
- Commit: 975028d46c880637dfe9b6d2b70e379b76052d09
- Version: 1.4.20 (CMakeLists.txt)
- License: Apache-2.0, complete text in LICENSE.txt.
- Files: src/YomkServerPy.py, src/YomkApi.py; version module generated from upstream CMake version.

Local changes: add module docstrings, change the facade's two imports to relative
imports, and remove the core module's logging.basicConfig call so importing the
framework does not configure LAN Mesh's process-wide logging. No service logic
is changed. Use the upstream Python sources directly; no Cython binary or ABI
dependency is required on Windows/Linux. Runtime dependency: readerwriterlock==1.0.9.

Do not use the facade's process-global init/shutdown in LAN Mesh. Each
ServiceRuntime owns YomkServer.create(), built-in Context and ServerInfo, and
its shutdown. Framework source updates require rechecking these local changes
and rerunning lifecycle, isolation, transport contract and full regression tests.
