#!/usr/bin/env python3
import os
import sys
import glob
import select
from typing import List
from dotenv import load_dotenv, set_key
from multiprocessing import Pool
from tqdm import tqdm

from langchain.document_loaders import (
    CSVLoader,
    EverNoteLoader,
    PDFMinerLoader,
    TextLoader,
    UnstructuredEmailLoader,
    UnstructuredEPubLoader,
    UnstructuredHTMLLoader,
    UnstructuredMarkdownLoader,
    UnstructuredODTLoader,
    UnstructuredPowerPointLoader,
    UnstructuredWordDocumentLoader,
)

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.vectorstores import Chroma
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.docstore.document import Document
from constants import CHROMA_SETTINGS


load_dotenv()


# Load environment variables
persist_directory = os.environ.get('PERSIST_DIRECTORY')
source_directory = os.environ.get('SOURCE_DIRECTORY', 'source_documents')
embeddings_model_name = os.environ.get('EMBEDDINGS_MODEL_NAME')
chunk_size = 500
chunk_overlap = 50


# Custom document loaders
class MyElmLoader(UnstructuredEmailLoader):
    """Wrapper to fallback to text/plain when default does not work"""

    def load(self) -> List[Document]:
        """Wrapper adding fallback for elm without html"""
        try:
            try:
                doc = UnstructuredEmailLoader.load(self)
            except ValueError as e:
                if 'text/html content not found in email' in str(e):
                    # Try plain text
                    self.unstructured_kwargs["content_source"]="text/plain"
                    doc = UnstructuredEmailLoader.load(self)
                else:
                    raise
        except Exception as e:
            # Add file_path to exception message
            raise type(e)(f"{self.file_path}: {e}") from e

        return doc


# Map file extensions to document loaders and their arguments
LOADER_MAPPING = {
    ".csv": (CSVLoader, {}),
    # ".docx": (Docx2txtLoader, {}),
    ".doc": (UnstructuredWordDocumentLoader, {}),
    ".docx": (UnstructuredWordDocumentLoader, {}),
    ".enex": (EverNoteLoader, {}),
    ".eml": (MyElmLoader, {}),
    ".epub": (UnstructuredEPubLoader, {}),
    ".html": (UnstructuredHTMLLoader, {}),
    ".md": (UnstructuredMarkdownLoader, {}),
    ".odt": (UnstructuredODTLoader, {}),
    ".pdf": (PDFMinerLoader, {}),
    ".ppt": (UnstructuredPowerPointLoader, {}),
    ".pptx": (UnstructuredPowerPointLoader, {}),
    ".txt": (TextLoader, {"encoding": "utf8"}),
    # Add more mappings for other file extensions and loaders as needed
}


def load_single_document(file_path: str) -> Document:
    ext = "." + file_path.rsplit(".", 1)[-1]
    if ext in LOADER_MAPPING:
        loader_class, loader_args = LOADER_MAPPING[ext]
        loader = loader_class(file_path, **loader_args)
        return loader.load()[0]

    raise ValueError(f"Unsupported file extension '{ext}'")


def load_documents(source_dir: str, ignored_files: List[str] = []) -> List[Document]:
    """
    Loads all documents from the source documents directory, ignoring specified files
    """
    all_files = []
    for ext in LOADER_MAPPING:
        all_files.extend(
            glob.glob(os.path.join(source_dir, f"**/*{ext}"), recursive=True)
        )
    filtered_files = [file_path for file_path in all_files if file_path not in ignored_files]

    with Pool(processes=os.cpu_count()) as pool:
        results = []
        with tqdm(total=len(filtered_files), desc='Loading new documents', ncols=80) as pbar:
            for i, doc in enumerate(pool.imap_unordered(load_single_document, filtered_files)):
                results.append(doc)
                pbar.update()

    return results

def process_documents(ignored_files: List[str] = []) -> List[Document]:
    """
    Load documents and split in chunks
    """
    print(f"Loading documents from {source_directory}")
    documents = load_documents(source_directory, ignored_files)
    if not documents:
        print("No new documents to load")
        exit(0)
    print(f"Loaded {len(documents)} new documents from {source_directory}")
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    texts = text_splitter.split_documents(documents)
    print(f"Split into {len(texts)} chunks of text (max. {chunk_size} tokens each)")
    return texts

def does_vectorstore_exist(persist_directory: str) -> bool:
    """
    Checks if vectorstore exists
    """
    if os.path.exists(os.path.join(persist_directory, 'index')):
        if os.path.exists(os.path.join(persist_directory, 'chroma-collections.parquet')) and os.path.exists(os.path.join(persist_directory, 'chroma-embeddings.parquet')):
            list_index_files = glob.glob(os.path.join(persist_directory, 'index/*.bin'))
            list_index_files += glob.glob(os.path.join(persist_directory, 'index/*.pkl'))
            # At least 3 documents are needed in a working vectorstore
            if len(list_index_files) > 3:
                return True
    return False


def prompt_user():
    """
    This function prompts the user to select an existing directory or create a new one to store source material.
    If an existing directory is selected, it checks if the directory is empty and prompts the user to create files
    in the directory if it is empty. It sets the directory paths as environment variables and returns them.

    Returns:
    selected_directory_path (str): The path of the selected directory.
    selected_db_path (str): The path of the database directory for the selected directory.
    """

    def _get_user_choice(timeout):
        if not isinstance(timeout, int):
            raise ValueError("Timeout value should be an integer.")
        print("Select an option or 'q' to quit:")
        print("1. Select an existing directory")
        print("2. Create a new directory")
        print(f"3. Use current source_directory: {source_directory}")
        inputs = [sys.stdin]
        readable, _, _ = select.select(inputs, [], [], timeout)
        if readable:
            return sys.stdin.readline().strip()
        return "3"

    def _display_directories():
        """
        This function displays the list of existing directories in the ./sources directory.
        """
        print("\n\033[94mExisting directories in ./sources:\033[0m")
        directories = sorted((file for file in os.listdir("./sources") if (os.path.isdir(os.path.join("./sources", file)) and not file.startswith("."))), key=str.lower)
        for index, directory in enumerate(directories, start=1):
            print(f"{index}. {directory}")
        return directories

    def _create_directory(directory_name):
        """
        This function creates a new directory with the given directory_name in the ./sources directory.
        It also creates a corresponding directory in the ./dbs directory for the database files.
        It sets the directory paths as environment variables and returns them.

        Parameters:
        directory_name (str): The name for the new directory.

        Returns:
        directory_path (str): The path of the new directory.
        db_path (str): The path of the database directory for the new directory.
        """
        directory_path = f"./sources/{directory_name}"
        db_path = f"./dbs/{directory_name}"
        os.makedirs(directory_path)
        os.makedirs(db_path)
        set_key('.env', 'SOURCE_DIRECTORY', directory_path)
        set_key('.env', 'PERSIST_DIRECTORY', db_path)
        print(f"Created new directory: {directory_path}")
        return directory_path, db_path

    while True:
        choice = _get_user_choice(timeout=5)
        if choice == "1":
            directories = _display_directories()
            existing_directory = input("Enter the number of the existing directory: ")
            try:
                selected_directory = directories[int(existing_directory) - 1]
                selected_directory_path = f"./sources/{selected_directory}"
                selected_db_path = f"./dbs/{selected_directory}"
                if not os.listdir(selected_directory_path):
                    print(f"Error: Directory '{selected_directory}' is empty.")
                    print("Please create files in the directory or choose another.")
                else:
                    if not os.path.exists(selected_db_path):
                        os.makedirs(selected_db_path)
                    set_key('.env', 'SOURCE_DIRECTORY', selected_directory_path)
                    set_key('.env', 'PERSIST_DIRECTORY', selected_db_path)
                    print(f"Selected directory: {selected_directory_path}")
                    break
            except (ValueError, IndexError):
                print("Invalid directory number. Please try again.")
        elif choice == "2":
            new_directory_name = input("Enter the name for the new directory: ")
            selected_directory_path, selected_db_path = _create_directory(new_directory_name)
            input("Place your source material into the new folder and press enter to continue...")
            break
        elif choice == "3":
            return source_directory, persist_directory
        elif choice == "q":
            exit(0)
        else:
            print("Invalid choice. Please try again.")

    return selected_directory_path, selected_db_path


def main():
    # Create embeddings
    embeddings = HuggingFaceEmbeddings(model_name=embeddings_model_name)

    if does_vectorstore_exist(persist_directory):
        # Update and store locally vectorstore
        print(f"Appending to existing vectorstore at {persist_directory}")
        db = Chroma(persist_directory=persist_directory, embedding_function=embeddings, client_settings=CHROMA_SETTINGS)
        collection = db.get()
        texts = process_documents([metadata['source'] for metadata in collection['metadatas']])
        print(f"Creating embeddings. May take some minutes...")
        db.add_documents(texts)
    else:
        # Create and store locally vectorstore
        print("Creating new vectorstore")
        texts = process_documents()
        print(f"Creating embeddings. May take some minutes...")
        db = Chroma.from_documents(texts, embeddings, persist_directory=persist_directory, client_settings=CHROMA_SETTINGS)
    db.persist()
    db = None

    print(f"Ingestion complete! You can now run privateGPT.py to query your documents")


if __name__ == "__main__":

    source_directory, persist_directory = prompt_user()

    main()
