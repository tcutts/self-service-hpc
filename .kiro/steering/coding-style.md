---
inclusion: always
---
Follow Clean Code principles.

When creating files, ALWAYS use the file-writing tools (fsWrite, fsAppend, strReplace). NEVER paste large content into shell commands (e.g. cat << 'EOF', echo, printf). Large pastes into the terminal are unreliable and will fail silently or corrupt content. Reserve shell commands for running builds, tests, and short CLI operations (<1024 characters) only.

<!------------------------------------------------------------------------------------
   Add rules to this file or a short description and have Kiro refine them for you.
   
   Learn about inclusion modes: https://kiro.dev/docs/steering/#inclusion-modes
-------------------------------------------------------------------------------------> 