from __future__ import annotations

from pathlib import Path
from shutil import copytree

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithRuntimeResources(build_py):
    """Include repository-level migrations and Mini App assets in built wheels."""

    resource_names = ("migrations", "mini_app")

    def run(self) -> None:
        super().run()
        repository_root = Path(__file__).parent
        package_root = Path(self.build_lib) / "procurement_bot" / "_resources"
        for name in self.resource_names:
            source = repository_root / name
            destination = package_root / name
            copytree(source, destination, dirs_exist_ok=True)

    def get_outputs(self, include_bytecode: int = 1) -> list[str]:
        outputs = super().get_outputs(include_bytecode)
        repository_root = Path(__file__).parent
        package_root = Path(self.build_lib) / "procurement_bot" / "_resources"
        for name in self.resource_names:
            source = repository_root / name
            destination = package_root / name
            outputs.extend(
                str(destination / path.relative_to(source))
                for path in source.rglob("*")
                if path.is_file()
            )
        return outputs


setup(cmdclass={"build_py": BuildPyWithRuntimeResources})
