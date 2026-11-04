import os
import ast
import re
from typing import List, Dict
from core.repository import RepositoryInventory, safe_path

class ProjectAnalyzer:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.nodes = []
        self.edges = []
        self.ignored_dirs = {".git", "node_modules", "__pycache__", "venv", "dist", "build", "workspaces", "results"}
        self.supported_exts = {".py", ".js", ".jsx", ".ts", ".tsx", ".c", ".cpp", ".h", ".hpp"}

    def _get_files(self) -> List[str]:
        self.inventory = RepositoryInventory(self.repo_path)
        if self.inventory.size == 'large':
            return []
        return self.inventory.source_files()

    def _extract_imports(self, rel_path: str) -> List[str]:
        full_path = os.path.join(self.repo_path, rel_path)
        imports = []
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(self.inventory.policy.max_file_bytes + 1)

            if rel_path.endswith(".py"):
                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imports.append(alias.name)
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            imports.append('.' * node.level + node.module)
            
            elif rel_path.endswith((".js", ".jsx", ".ts", ".tsx")):
                # Regex for JS/TS imports
                matches = re.findall(r'import\s+.*\s+from\s+[\'"](.+)[\'"]', content)
                matches += re.findall(r'require\([\'"](.+)[\'"]\)', content)
                for m in matches:
                    if m.startswith("."):
                        # Resolve relative path simple logic
                        imports.append(m)
                    else:
                        imports.append(m)
            
            elif rel_path.endswith((".c", ".cpp", ".h", ".hpp")):
                # Regex for C++ includes
                matches = re.findall(r'#include\s+["<](.+)[">]', content)
                imports.extend(matches)

        except Exception:
            pass
        return imports

    def analyze(self) -> Dict:
        files = self._get_files()
        file_set = set(files)
        suffix_index = {}
        for path in files:
            parts = path.split('/')
            for index in range(len(parts)):
                suffix_index.setdefault('/'.join(parts[index:]), []).append(path)
        
        # Map basenames for easier matching
        basename_map = {}
        for f in files:
            bn = os.path.splitext(os.path.basename(f))[0]
            if bn not in basename_map:
                basename_map[bn] = []
            basename_map[bn].append(f)

        nodes = []
        edges = []
        counts = {f: {"in": 0, "out": 0, "size": os.path.getsize(os.path.join(self.repo_path, f))} for f in files}

        for f in files:
            imports = self._extract_imports(f)
            for imp in imports:
                # Try to resolve import to a file in our set
                target = None
                
                # 1. Direct rel path match
                if imp in file_set:
                    target = imp
                else:
                    # Resolve local paths first, then use indexed unambiguous suffixes.
                    if f.endswith('.py'):
                        depth = len(imp) - len(imp.lstrip('.'))
                        base = os.path.dirname(f)
                        for _ in range(max(0, depth - 1)):
                            base = os.path.dirname(base)
                        module = imp.lstrip('.').replace('.', '/')
                        candidate = os.path.join(base, module) if depth else module
                    else:
                        candidate = os.path.join(os.path.dirname(f), imp) if imp.startswith('.') else imp
                    candidate = os.path.normpath(candidate).replace('\\', '/')
                    suffixes = ['', '.py', '/__init__.py', '.js', '.jsx', '.ts', '.tsx', '/index.js', '/index.ts', '/index.tsx', '.cpp', '.c', '.h']
                    for suffix in suffixes:
                        key = candidate + suffix
                        if key in file_set:
                            target = key
                            break
                        matches = suffix_index.get(key, [])
                        if len(matches) == 1:
                            target = matches[0]
                            break
                    
                    if not target:
                        # 3. Last fallback: Basename match
                        bn = os.path.splitext(imp.replace('\\', '/').split('/')[-1])[0]
                        if bn in basename_map and len(basename_map[bn]) == 1:
                            target = basename_map[bn][0]
                
                if target and target != f:
                    edges.append({"source": f, "target": target})
                    counts[f]["out"] += 1
                    counts[target]["in"] += 1

        for f in files:
            # Risk Score calculation
            # High in-degree = Core (lower risk if stable, but high impact)
            # High out-degree = Complex (higher risk)
            # Large size = Higher risk
            score = (counts[f]["out"] * 0.1) + (counts[f]["in"] * 0.05) + (min(counts[f]["size"] / 10000, 0.5))
            score = round(min(score, 1.0), 2)
            
            nodes.append({
                "id": f,
                "type": "file",
                "risk_score": score
            })

        return {"nodes": nodes, "edges": edges, 'repository_profile': self.inventory.summary()}
