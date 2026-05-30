"""FastAPI backend for MMGraphRAG ingestion, query, and graph visualization."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import parameter
from ..builder import MMKGBuilder
from ..parameter import QueryParam
from ..retrieval.query import (
    GraphRAGQuery,
    clear_runtime_caches,
    find_similar_nodes,
    load_or_build_embeddings,
    read_graphml,
)

logger = logging.getLogger(__name__)


@dataclass
class RuntimeConfig:
    working_dir: str
    output_dir: str
    upload_dir: str
    graph_path: str | None = None
    use_mineru: bool | None = None


class QueryRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=50)
    response_type: str | None = None


_graphml_cache: dict[str, tuple[float, dict]] = {}


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _apply_runtime_config(config: RuntimeConfig) -> None:
    parameter.WORKING_DIR = config.working_dir
    parameter.OUTPUT_DIR = config.output_dir
    if config.use_mineru is not None:
        parameter.USE_MINERU = config.use_mineru
    _ensure_dir(config.working_dir)
    _ensure_dir(config.output_dir)
    _ensure_dir(config.upload_dir)


def _clear_graph_cache() -> None:
    _graphml_cache.clear()
    clear_runtime_caches()


def _clear_directory(path: str) -> None:
    if not os.path.exists(path):
        return

    for entry in os.listdir(path):
        entry_path = os.path.join(path, entry)
        if os.path.isdir(entry_path):
            shutil.rmtree(entry_path)
        else:
            os.remove(entry_path)


def _reset_storage(config: RuntimeConfig, clear_uploads: bool) -> None:
    _clear_directory(config.working_dir)
    _clear_directory(config.output_dir)
    if clear_uploads:
        _clear_directory(config.upload_dir)
    _clear_graph_cache()


def _find_graphml(config: RuntimeConfig) -> str | None:
    if config.graph_path and os.path.exists(config.graph_path):
        return config.graph_path

    if not os.path.isdir(config.output_dir):
        return None

    graphml_files = [
        os.path.join(config.output_dir, name)
        for name in os.listdir(config.output_dir)
        if name.endswith('.graphml')
    ]
    if not graphml_files:
        return None

    mmkg_files = [path for path in graphml_files if 'mmkg' in os.path.basename(path).lower()]
    candidates = mmkg_files or graphml_files
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


def parse_graphml(filepath: str) -> dict | None:
    """Parse GraphML and return node and edge data for the UI."""
    if filepath in _graphml_cache:
        cache_time, data = _graphml_cache[filepath]
        if os.path.exists(filepath) and os.path.getmtime(filepath) <= cache_time:
            return data

    if not os.path.exists(filepath):
        return None

    nodes = []
    edges = []
    entity_types = {}

    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        ns = {'graphml': 'http://graphml.graphdrawing.org/xmlns'}

        key_map = {}
        for key in root.findall('graphml:key', ns):
            key_map[key.get('id')] = key.get('attr.name')

        graph = root.find('graphml:graph', ns)
        if graph is None:
            return None

        for node in graph.findall('graphml:node', ns):
            node_id = node.get('id', '').strip('"')
            node_data = {'id': node_id}
            for data in node.findall('graphml:data', ns):
                key_id = data.get('key')
                key_name = key_map.get(key_id, key_id)
                node_data[key_name] = (data.text or '').strip('"')

            entity_type = node_data.get('entity_type', 'UNKNOWN').strip('"')
            node_data['entity_type'] = entity_type
            entity_types[entity_type] = entity_types.get(entity_type, 0) + 1
            nodes.append(node_data)

        for edge in graph.findall('graphml:edge', ns):
            edge_data = {
                'source': edge.get('source', '').strip('"'),
                'target': edge.get('target', '').strip('"'),
            }
            for data in edge.findall('graphml:data', ns):
                key_id = data.get('key')
                key_name = key_map.get(key_id, key_id)
                edge_data[key_name] = (data.text or '').strip('"')
            edges.append(edge_data)

        result = {
            'nodes': nodes,
            'edges': edges,
            'entity_types': entity_types,
            'node_count': len(nodes),
            'edge_count': len(edges),
        }
        _graphml_cache[filepath] = (os.path.getmtime(filepath), result)
        return result
    except Exception as exc:
        logger.error('Failed to parse GraphML: %s', exc)
        return None


def _serialize_hit_node(node: dict) -> dict:
    entity_name = node.get('entity_name', '')
    return {
        'id': entity_name.strip('"'),
        'entity_name': entity_name,
        'entity_type': node.get('entity_type', 'UNKNOWN'),
        'description': node.get('description', ''),
        'rank': node.get('rank', 0),
    }


def _serialize_hit_edge(edge: dict) -> dict:
    return {
        'source': edge.get('source', '').strip('"'),
        'target': edge.get('target', '').strip('"'),
        'description': edge.get('description', ''),
        'weight': edge.get('weight', 1.0),
        'rank': edge.get('rank', 0),
    }


def create_app(config: RuntimeConfig) -> FastAPI:
    _apply_runtime_config(config)

    app = FastAPI(title='MMGraphRAG API')
    app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )
    app.state.config = config
    app.state.runtime_lock = asyncio.Lock()

    static_dir = Path(__file__).resolve().parent
    index_file = static_dir / 'graph_explorer.html'

    def get_graph_path_or_404() -> str:
        graphml_path = _find_graphml(app.state.config)
        if not graphml_path:
            raise HTTPException(status_code=404, detail='No graph file found. Ingest a PDF first.')
        return graphml_path

    @app.get('/')
    async def index() -> FileResponse:
        return FileResponse(index_file)

    @app.get('/api/health')
    async def health() -> dict:
        graphml_path = _find_graphml(app.state.config)
        return {
            'success': True,
            'working_dir': app.state.config.working_dir,
            'output_dir': app.state.config.output_dir,
            'upload_dir': app.state.config.upload_dir,
            'graph_available': graphml_path is not None,
            'graph_path': graphml_path,
        }

    @app.get('/api/graph/info')
    async def graph_info() -> dict:
        graphml_path = get_graph_path_or_404()
        data = parse_graphml(graphml_path)
        if not data:
            raise HTTPException(status_code=500, detail='Failed to parse graph')

        return {
            'success': True,
            'path': graphml_path,
            'node_count': data['node_count'],
            'edge_count': data['edge_count'],
            'entity_types': data['entity_types'],
            'size': os.path.getsize(graphml_path),
        }

    @app.get('/api/graph/content')
    async def graph_content(limit: int = Query(default=5000, ge=1, le=20000)) -> dict:
        graphml_path = get_graph_path_or_404()
        data = parse_graphml(graphml_path)
        if not data:
            raise HTTPException(status_code=500, detail='Failed to parse graph')

        nodes = data['nodes'][:limit]
        node_ids = {node['id'] for node in nodes}
        edges = [
            edge
            for edge in data['edges']
            if edge['source'] in node_ids and edge['target'] in node_ids
        ]
        return {
            'success': True,
            'nodes': nodes,
            'edges': edges,
            'entity_types': data['entity_types'],
            'total_nodes': data['node_count'],
            'total_edges': data['edge_count'],
            'has_more': len(data['nodes']) > limit,
        }

    @app.get('/api/graph/search')
    async def graph_search(q: str = '') -> dict:
        query = q.lower().strip()
        if not query:
            return {'success': True, 'results': []}

        graphml_path = get_graph_path_or_404()
        data = parse_graphml(graphml_path)
        if not data:
            raise HTTPException(status_code=500, detail='Failed to parse graph')

        results = []
        for node in data['nodes']:
            node_id = node.get('id', '')
            node_id_lower = node_id.lower()
            description = node.get('description', '').lower()

            score = None
            if node_id_lower == query:
                score = 0
            elif node_id_lower.startswith(query):
                score = 1 + len(node_id) * 0.001
            elif query in node_id_lower:
                score = 2 + len(node_id) * 0.001
            elif query in description:
                score = 3 + len(node_id) * 0.001

            if score is not None:
                results.append({
                    'id': node_id,
                    'entity_type': node.get('entity_type', 'UNKNOWN'),
                    'description': node.get('description', '')[:200],
                    '_score': score,
                })

        results.sort(key=lambda item: item['_score'])
        trimmed = []
        for item in results[:50]:
            item.pop('_score', None)
            trimmed.append(item)
        return {'success': True, 'results': trimmed}

    @app.get('/api/graph/retrieve')
    async def graph_retrieve(q: str = '') -> dict:
        query = q.strip()
        if not query:
            raise HTTPException(status_code=400, detail='Please enter a retrieval query')

        graphml_path = get_graph_path_or_404()
        graph = read_graphml(graphml_path)
        embeddings = load_or_build_embeddings(graph, graphml_path)
        similar_nodes = find_similar_nodes(query, embeddings, parameter.RETRIEVAL_THRESHOLD, top_k=20)

        if not similar_nodes:
            return {'success': True, 'nodes': [], 'edges': [], 'message': 'No related nodes found'}

        node_names_raw = {node['entity_name'] for node in similar_nodes}
        related_edges = []
        for node_name in node_names_raw:
            if not graph.has_node(node_name):
                continue
            for source, target in graph.edges(node_name):
                if source in node_names_raw and target in node_names_raw:
                    related_edges.append({
                        'source': source.strip('"'),
                        'target': target.strip('"'),
                    })

        seen = set()
        unique_edges = []
        for edge in related_edges:
            key = tuple(sorted((edge['source'], edge['target'])))
            if key not in seen:
                seen.add(key)
                unique_edges.append(edge)

        return {
            'success': True,
            'nodes': [name.strip('"') for name in node_names_raw],
            'edges': unique_edges,
            'scores': {node['entity_name'].strip('"'): node['score'] for node in similar_nodes},
        }

    @app.post('/api/documents/ingest')
    async def ingest_document(file: UploadFile = File(...)) -> dict:
        if not file.filename or not file.filename.lower().endswith('.pdf'):
            raise HTTPException(status_code=400, detail='Only PDF uploads are supported')

        config = app.state.config
        _apply_runtime_config(config)
        saved_path = os.path.join(config.upload_dir, os.path.basename(file.filename))

        async with app.state.runtime_lock:
            _reset_storage(config, clear_uploads=False)
            with open(saved_path, 'wb') as handle:
                shutil.copyfileobj(file.file, handle)

            builder = MMKGBuilder(working_dir=config.working_dir)
            await builder.aindex(saved_path)
            _clear_graph_cache()

        graphml_path = _find_graphml(config)
        if not graphml_path:
            raise HTTPException(status_code=500, detail='Graph build completed but no GraphML file was produced')

        data = parse_graphml(graphml_path)
        return {
            'success': True,
            'file_name': os.path.basename(saved_path),
            'graph_path': graphml_path,
            'node_count': data['node_count'] if data else 0,
            'edge_count': data['edge_count'] if data else 0,
        }

    @app.delete('/api/storage')
    async def clear_storage() -> dict:
        config = app.state.config
        async with app.state.runtime_lock:
            _reset_storage(config, clear_uploads=True)
            _apply_runtime_config(config)
        return {'success': True, 'message': 'Working, output, and upload data cleared'}

    @app.post('/api/query')
    async def query_graph(payload: QueryRequest) -> dict:
        config = app.state.config
        _apply_runtime_config(config)
        graphml_path = get_graph_path_or_404()

        async with app.state.runtime_lock:
            params = QueryParam()
            if payload.top_k is not None:
                params.top_k = payload.top_k
            if payload.response_type:
                params.response_type = payload.response_type

            rag = GraphRAGQuery(working_dir=config.working_dir, output_dir=config.output_dir)
            details = await rag.query_with_details(payload.query, params)

        return {
            'success': True,
            'query': payload.query,
            'graph_path': graphml_path,
            'answer': details.response,
            'hit_nodes': [_serialize_hit_node(node) for node in details.hit_nodes],
            'hit_edges': [_serialize_hit_edge(edge) for edge in details.hit_edges],
            'source_chunks': details.source_chunks,
            'multimodal_entities': details.multimodal_entities,
            'entities_context': details.entities_context,
            'full_context': details.full_context,
        }

    return app


def run_api_server(
    working_dir: str,
    output_dir: str,
    upload_dir: str,
    port: int = 8000,
    host: str = '0.0.0.0',
    graph_path: str | None = None,
    use_mineru: bool | None = None,
) -> None:
    config = RuntimeConfig(
        working_dir=working_dir,
        output_dir=output_dir,
        upload_dir=upload_dir,
        graph_path=graph_path,
        use_mineru=use_mineru,
    )
    app = create_app(config)

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level='info')
