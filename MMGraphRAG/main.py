"""Entry point for the MMGraphRAG FastAPI application."""

import argparse
import logging
import os
import sys
import warnings

os.environ['YOLO_VERBOSE'] = 'False'
warnings.filterwarnings('ignore')

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src import parameter
from src.visualization.server import run_api_server

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S',
    level=logging.INFO,
)
logger = logging.getLogger('main')
logging.getLogger('ultralytics').setLevel(logging.ERROR)


def main() -> None:
    parser = argparse.ArgumentParser(description='Run the MMGraphRAG FastAPI application')
    parser.add_argument('-w', '--working', dest='working_dir', default='app_data/working')
    parser.add_argument('-o', '--output', dest='output_dir', default='app_data/output')
    parser.add_argument('-u', '--upload-dir', dest='upload_dir', default='app_data/uploads')
    parser.add_argument('-m', '--method', choices=['mineru', 'pymupdf'], default=None)
    parser.add_argument('--graph', dest='graph_path', default=None)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()

    parameter.WORKING_DIR = args.working_dir
    parameter.OUTPUT_DIR = args.output_dir
    if args.method:
        parameter.USE_MINERU = args.method == 'mineru'

    logger.info('Starting MMGraphRAG FastAPI application')
    logger.info('Working directory: %s', args.working_dir)
    logger.info('Output directory: %s', args.output_dir)
    logger.info('Upload directory: %s', args.upload_dir)
    logger.info('Port: %s', args.port)

    run_api_server(
        working_dir=args.working_dir,
        output_dir=args.output_dir,
        upload_dir=args.upload_dir,
        port=args.port,
        host=args.host,
        graph_path=args.graph_path,
        use_mineru=parameter.USE_MINERU,
    )


if __name__ == '__main__':
    main()
