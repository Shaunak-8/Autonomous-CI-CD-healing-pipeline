import os
import ast

class DependencyGraph:
    def __init__(self):
        self.nodes = []
        self.edges = []
        self._adjacency = {}

    def _extract_imports_from_file(self, file_path: str) -> list:
        """Parse a Python file and extract imported module names."""
        imports = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                source = f.read()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.append(node.module.split(".")[0])
        except (SyntaxError, FileNotFoundError, OSError):
            pass
        return imports

    def build_from_failures(self, failures: list, repo_path: str = ""):
        # Order-preserving deduplication
        self.nodes = list(dict.fromkeys(f["file"] for f in failures))
        self.edges = []
        self._adjacency = {node: [] for node in self.nodes}

        node_basenames = {os.path.splitext(os.path.basename(n))[0]: n for n in self.nodes}

        for node in self.nodes:
            file_path = os.path.join(repo_path, node) if repo_path else node
            imported_modules = self._extract_imports_from_file(file_path)
            for mod in imported_modules:
                if mod in node_basenames and node_basenames[mod] != node:
                    target = node_basenames[mod]
                    if target not in self._adjacency[node]:
                        self._adjacency[node].append(target)
                        self.edges.append({"source": node, "target": target})

    def get_root_nodes(self) -> list:
        # Edges point from an importer to its dependency: dependencies are fixed first.
        return [node for node in self.nodes if not self._adjacency[node]]

    def get_blast_radius(self, node_id: str) -> int:
        if node_id not in self._adjacency:
            raise ValueError(f"Unknown node_id: {node_id}. Valid nodes: {self.nodes}")

        visited = set()

        dependents = {node: [] for node in self.nodes}
        for source, targets in self._adjacency.items():
            for target in targets:
                dependents[target].append(source)
        pending = [node_id]
        while pending:
            node = pending.pop()
            if node not in visited:
                visited.add(node)
                pending.extend(dependents.get(node, []))
        return max(0, len(visited) - 1)

    def to_dict(self) -> dict:
        return {
            "nodes": list(self.nodes),
            "edges": list(self.edges),
        }
