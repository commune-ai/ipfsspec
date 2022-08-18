import streamlit as st
import fsspec
from ipfsspec.asyn import AsyncIPFSFileSystem
from fsspec import register_implementation
from ipfsspec.utils import dict_equal, dict_hash
import asyncio
import io
import os
from datasets import load_dataset, Dataset






# register_implementation(IPFSFileSystem.protocol, IPFSFileSystem)
register_implementation(AsyncIPFSFileSystem.protocol, AsyncIPFSFileSystem)

class fs:
    ipfs = fsspec.filesystem("ipfs")
    local = fsspec.filesystem('file')

test_dir = '/tmp/test'

ds = load_dataset("rotten_tomatoes", split="validation")
st.write(ds.save_to_disk(dataset_path=test_dir+'/test_ds' , fs=fs.ipfs))

st.write(fs.local.ls(test_dir+'/test_ds'))



# with fs.local.open('', mode='w')
