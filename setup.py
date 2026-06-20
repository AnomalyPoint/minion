from setuptools import setup, find_packages

setup(
    name="minion",
    version="0.1.1",
    description="A deliberately tiny coding agent for self-hosted models.",
    license="MIT",
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
    ],
    py_modules=["minion"],          # tells setuptools to package the single .py file
    python_requires=">=3.9",
    install_requires=[
        "openai",                   # the only runtime dep, per the file's docstring
        "httpx<0.28",               # openai<1.55 passes proxies=; httpx>=0.28 removed it
    ],
    entry_points={
        "console_scripts": [
            "minion=minion:main",   # creates a `minion` command that calls minion.main()
        ],
    },
)
