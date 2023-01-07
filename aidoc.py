import argparse
import ast
import dataclasses
import datetime
import glob
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple, Union

import astor
import black
import openai

logger = logging.getLogger("aidocgen")
handler = logging.StreamHandler()
logger.addHandler(handler)
logger.setLevel(logging.INFO)

CONFIG_DIR = Path("~/.config/aidocgen")
API_KEY, MODEL = None, None
DEFAULT_MODEL = "code-davinci-002"


def cli():
    parser = argparse.ArgumentParser(
        description="""Document your code automatically using AI."""
    )

    # Create a parser for the gen command
    subcommands = parser.add_subparsers(dest="command")
    gen_parser = subcommands.add_parser(
        "gen", help="generate documentation for source file"
    )

    # Add the source_file argument to the gen parser
    gen_parser.add_argument(
        "source_file",
        type=Path,
        help="path to the source file or directory(recursive)",
    )
    gen_parser.add_argument(
        "-o",
        "--overwrite",
        action="store_true",
        help="overwrite existing docstrings",
    )
    gen_parser.add_argument(
        "-f",
        "--format",
        action="store_true",
        default=True,
        help="format the entire source file using black (default=True)",
    )
    gen_parser.add_argument(
        "-pr",
        "--pull-request",
        action="store_true",
        help="create a pull request with the changes",
    )

    # Create a parser for the configure command
    subcommands.add_parser("configure", help="configure API key and model")

    args = parser.parse_args()
    return args


@dataclass
class ExtractedFunction:
    """Extracted function from a source file

    Parameters
    ----------
    name : str
        Function name
    args : List[str]
        List of function arguments
    returns : Optional[str]
        Return type
    docstring : Optional[str]
        Function docstring
    code : str
        Code snippet
    is_docstring_generated : bool
        True if the docstring was generated by GPT-3, False otherwise
    is_code_updated : bool
        True if the code was updated with the generated docstring, False otherwise
    """

    name: str
    args: List[str] = dataclasses.field(default_factory=list)
    returns: Optional[str] = None
    docstring: Optional[str] = None
    code: str = ""
    is_docstring_generated: bool = False
    is_code_updated: bool = False


@dataclass
class ExtractedClass:
    """Extracted class from a source file

    Parameters
    ----------
    name : str
        Class name
    methods : List[ExtractedFunction]
        List of methods in the class
    docstring : Optional[str]
        Class docstring
    code : str
        Code snippet
    is_docstring_generated : bool
        True if the docstring was generated by GPT-3, False otherwise
    is_code_updated : bool
        True if the code was updated with the generated docstring, False otherwise
    """

    name: str
    methods: List[ExtractedFunction] = dataclasses.field(default_factory=list)
    docstring: Optional[str] = None
    code: str = ""
    is_docstring_generated: bool = False
    is_code_updated: bool = False


def read_source_file(source_path: Path) -> str:
    """Reads a source file and returns the source code.

    Parameters
    ----------
    source_path : Path

    Returns
    -------
    source : str

    """
    with open(source_path, "r") as f:
        source = f.read()
    return source


def write_source_file(source_path: Path, updated_source: str) -> None:
    """Writes the updated source code to the source file.

    Parameters
    ----------
    source_path : Path
    updated_source : str

    Returns
    ----------
    None

    """
    with open(source_path, "w") as f:
        f.write(updated_source)


def extract(
    source: str,
) -> Tuple[List[ExtractedFunction], List[ExtractedClass]]:

    """
    Extracts functions and classes from a source file.

    Parameters
    ----------
    source : str
        Source code

    Returns
    -------
    functions : List[ExtractedFunction]
        List of functions in the source file
    classes : List[ExtractedClass]
        List of classes in the source file
    """

    tree = ast.parse(source)

    functions = []
    classes = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            functions.append(extract_function(node))
        if isinstance(node, ast.ClassDef):
            name = node.name
            methods = []
            docstring = ast.get_docstring(node)

            for child in node.body:
                if isinstance(child, ast.FunctionDef):
                    methods.append(extract_function(child))

            classes.append(
                ExtractedClass(
                    name=name,
                    methods=methods,
                    docstring=docstring,
                    code=astor.to_source(node),
                )
            )

    return functions, classes


def extract_function(node: ast.FunctionDef) -> ExtractedFunction:
    """
    Extracts information about a function.

    Parameters
    ----------
    node : ast.FunctionDef

    Returns
    -------
    functions : List[ExtractedFunction]
    """
    name = node.name
    args = [arg.arg for arg in node.args.args]
    returns = None
    if node.returns:
        returns = ast.unparse(node.returns).strip()

    docstring = ast.get_docstring(node)

    return ExtractedFunction(
        name=name,
        args=args,
        returns=returns,
        docstring=docstring,
        code=astor.to_source(node),
    )


def insert_docstring(
    source: str,
    function_or_class: Union[ExtractedFunction, ExtractedClass],
    overwrite=False,
) -> str:
    """Inserts a docstring into a function in a source code and returns the updated code string.

    Parameters
    ----------
    source_file : str
    function : ExtractedFunction

    Returns
    -------
    updated_source : str
    """

    tree = ast.parse(source)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef) or isinstance(node, ast.ClassDef)
        ) and node.name == function_or_class.name:
            docstring = ast.get_docstring(node)
            if docstring and len(docstring.strip()) > 0 and not overwrite:
                logger.info(
                    f"{node.name}'s docstring already exists. Skipping..."
                )
                continue
            delete_docstring(node)
            node.body.insert(0, ast.Expr(ast.Str(function_or_class.docstring)))
            break

    updated_source = astor.to_source(tree)
    return updated_source


def delete_docstring(node: ast.FunctionDef) -> None:
    """Deletes the docstring from a function/class definition.

    Parameters
    ----------
    node : ast.FunctionDef

    Returns
    -------
    None
    """
    docstring = ast.get_docstring(node)
    if docstring:
        node.body.pop(0)


def generate_docstring(code_snippet, object_type) -> Tuple[str, bool]:
    """Generates a docstring for a function using OpenAI's GPT-3 API.

    Parameters
    ----------
    code_snippet : str
        Code snippet for which the docstring is to be generated

    Returns
    -------
    docstring : str
        Generated docstring
    success : bool
        True if the docstring was generated successfully, False otherwise
    """

    openai.api_key = API_KEY
    openai_model = MODEL or "code-davinci-002"

    if object_type == "class":
        prompt = f"""# Python 3.7\n \n{code_snippet}\n\n# write a concise, high-quality docstring for the above {object_type} in Google style.  It must only have a one liner about the {object_type}:\n\"\"\""""
    else:
        prompt = f"""# Python 3.7\n \n{code_snippet}\n\n# write a concise, high-quality docstring for the above {object_type} in Google style.  It must have one liner about the {object_type}, 'Args' and 'Returns' (only if it's a function/method):\n\"\"\""""
    try:
        response = openai.Completion.create(
            model=openai_model,
            prompt=prompt,
            temperature=0,
            max_tokens=250,
            top_p=1.0,
            frequency_penalty=0.0,
            presence_penalty=0.0,
            stop=["#", '"""'],
        )
        return response.choices[0].text, True
    except Exception as e:
        logger.error(f"Unable to generate docstring\n error: {e}")
        return "", False


def process_file(source_file: Path, args: NamedTuple):
    """Processes a single file and generates docstrings for functions and classes.

    Parameters
    ----------
    source_file : Path
    args : NamedTuple

    Returns
    -------
    None
    """

    source = read_source_file(source_file)
    source_copy = source[:]
    functions, classes = extract(source)
    for function in functions:
        if function.name == "__init__":
            continue
        (
            function.docstring,
            function.is_docstring_generated,
        ) = generate_docstring(function.code, "function")
        if function.is_docstring_generated and len(function.docstring) > 0:
            source = insert_docstring(
                source, function, overwrite=args.overwrite
            )

    for class_ in classes:
        (
            class_.docstring,
            class_.is_docstring_generated,
        ) = generate_docstring(class_.code, "class")
        if class_.is_docstring_generated and len(function.docstring) > 0:
            source = insert_docstring(source, class_, overwrite=args.overwrite)

    write_source_file(source_file, source)

    if args.format:
        black.format_file_in_place(
            Path(source_file),
            fast=False,
            mode=black.FileMode(),
            write_back=black.WriteBack.YES,
        )

    if source != source_copy:
        logger.info(f"✅ Docstrings generated for {source_file}")
    else:
        logger.info(f"🙏 {source_file} unchanged.")

    if args.pull_request:
        create_pr(source_file)


def create_pr(source_file: Path) -> None:
    """Creates a PR with the changes made to the source file.

    Parameters
    ----------
    source_file : Path

    Returns
    -------
    None
    """

    source_file = source_file.as_posix()

    diff = os.popen(f"git diff {source_file}").read()
    if len(diff) == 0:
        logger.error(f"❌ Can't create PR.")
        return
    git_branch = f"add-docstrings-to-{source_file}-{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}"
    os.system(f"git checkout -b {git_branch}")
    os.system(f"git add {source_file}")
    os.system(f'git commit -m "add docstrings to {source_file}"')
    os.system(f"git push -u origin {git_branch}")
    os.system(
        f"gh pr create -f -t 'add docstrings to {source_file}' -b 'add docstrings to {source_file}'"
    )


def configure():
    logger.info("Please configure AIDocGen before using it...")
    api_key = None
    while not api_key:
        api_key = input("Enter the OpenAI API key: ")
    model = input(
        "Enter the OpenAI model to use (default: code-davinci-002): "
    )
    if len(model.strip()) == 0:
        model = DEFAULT_MODEL

    config_dir = CONFIG_DIR.expanduser()
    logger.info(f"configuration saved to {config_dir}")
    if not config_dir.exists():
        logger.info("Creating config directory...")
        config_dir.mkdir(parents=True)
    config_path = config_dir / "config.ini"
    with open(config_path, "w") as config_file:
        config_file.write(f"OPENAI_API_KEY={api_key}\n")
        config_file.write(f"OPENAI_MODEL={model}\n")

    return api_key, model


def main():
    """
    Usage: aidoc gen <source_file_or_directory> [options]
    Options:
        -h --help to see more options
    """
    global API_KEY, MODEL

    args = cli()

    if args.command is None:
        print(main.__doc__)
        return

    if args.command == "configure":
        configure()
        return

    API_KEY, MODEL = read_config()
    if not API_KEY and not MODEL:
        API_KEY, MODEL = configure()

    source_path = args.source_file

    if os.path.isfile(source_path):
        process_file(source_path, args)
    elif os.path.isdir(source_path):
        python_files = glob.glob(f"{source_path}/**/*.py", recursive=True)
        for python_file in python_files:
            process_file(python_file, args)


def read_config():
    """Reads the API key and model from the configuration file."""
    try:
        config_dir = CONFIG_DIR.expanduser()
        with open(config_dir / "config.ini", "r") as config_file:
            lines = config_file.readlines()
            api_key = lines[0].strip().split("=")[1]
            model = lines[1].strip().split("=")[1]
            return api_key, model
    except Exception as e:
        logger.error(f"Unable to read configuration file")
        return None, None


if __name__ == "__main__":
    main()
