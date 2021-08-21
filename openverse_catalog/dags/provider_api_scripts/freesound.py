"""
Content Provider:       Freesound

ETL Process:            Use the API to identify all CC-licensed images.

Output:                 TSV file containing the image, the respective
                        meta-data.

Notes:                  {{API URL}}
                        No rate limit specified.
"""
import os
import logging
from urllib.parse import urlparse

from common import DelayedRequester
from common import get_license_info
from common import AudioStore
from util.loader import provider_details as prov

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s:  %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

LIMIT = 150
DELAY = 1  # in seconds
RETRIES = 3
HOST = 'freesound.org'
ENDPOINT = f'https://{HOST}/apiv2/search/text'
PROVIDER = prov.FREESOUND_DEFAULT_PROVIDER
API_KEY = os.getenv(
    "FREESOUND_API_KEY",
    "not_set"
)

HEADERS = {
    "Accept": "application/json",
}
DEFAULT_QUERY_PARAMS = {
    'format': 'json',
    'token': API_KEY,
    'query': '',
    'page_size': LIMIT,
    'fields': 'id,url,name,tags,description,created,license,type,channels,'
              'filesize,bitrate,bitdepth,duration,samplerate,pack,username,'
              'images,num_downloads,avg_rating,num_ratings'
}

delayed_requester = DelayedRequester(DELAY)
audio_store = AudioStore(provider=PROVIDER)


def main():
    """
    This script pulls the data for a given date from the Freesound,
    and writes it into a .TSV file to be eventually read
    into our DB.
    """

    logger.info("Begin: Freesound script")
    licenses = [
        'Attribution',
        'Attribution Noncommercial',
        'Creative Commons 0'
    ]
    for license_name in licenses:
        image_count = _get_items(license_name)
        logger.info(f"Audios for {license_name} pulled: {image_count}")
    logger.info('Terminated!')


def _get_query_param(
        license_name='',
        page_number=1,
        default_query_param=None,
):
    if default_query_param is None:
        default_query_param = DEFAULT_QUERY_PARAMS
    query_param = default_query_param.copy()
    query_param["page"] = str(page_number)
    query_param['license'] = license_name
    return query_param


def _get_items(license_name):
    item_count = 0
    page_number = 1
    should_continue = True
    while should_continue:
        query_param = _get_query_param(
            license_name=license_name,
            page_number=page_number)
        batch_data = _get_batch_json(
            query_param=query_param
        )
        if isinstance(batch_data, list) and len(batch_data) > 0:
            item_count = _process_item_batch(batch_data)
            page_number += 1
        else:
            should_continue = False
    return item_count


def _get_batch_json(
        endpoint=ENDPOINT,
        headers=None,
        retries=RETRIES,
        query_param=None
):
    if headers is None:
        headers = HEADERS
    response_json = delayed_requester.get_response_json(
        endpoint,
        retries,
        query_param,
        headers=headers
    )
    if response_json is None:
        return None
    else:
        results = response_json.get("results")
        return results


def _process_item_batch(items_batch):
    for item in items_batch:
        item_meta_data = _extract_audio_data(item)
        if item_meta_data is None:
            continue
        audio_store.add_item(**item_meta_data)
    return audio_store.total_items


def _extract_audio_data(media_data):
    try:
        foreign_landing_url = media_data["url"]
    except (TypeError, KeyError, KeyError):
        return None
    audio_url, duration = _get_audio_info(media_data)
    if audio_url is None:
        return None
    item_license = _get_license(media_data)
    if item_license is None:
        return None
    foreign_identifier = _get_foreign_identifier(media_data)
    title = _get_title(media_data)
    creator, creator_url = _get_creator_data(media_data)
    thumbnail = _get_thumbnail_url(media_data)
    metadata = _get_metadata(media_data)
    tags = _get_tags(media_data)
    return {
        'title': title,
        'creator': creator,
        'creator_url': creator_url,
        'foreign_identifier': foreign_identifier,
        'foreign_landing_url': foreign_landing_url,
        'audio_url': audio_url,
        'duration': duration,
        'thumbnail_url': thumbnail,
        'license_': item_license.license,
        'license_version': item_license.version,
        'meta_data': metadata,
        'raw_tags': tags
    }


def _get_foreign_identifier(media_data):
    try:
        return media_data['id']
    except(TypeError, IndexError, KeyError):
        return None


def _get_audio_info(media_data):
    duration = media_data.get('duration')
    # TODO: Need oauth to get sample!
    # This URL is foreign_landing_url
    audio_url = media_data.get('url')
    return audio_url, duration


def _get_thumbnail_url(media_data):
    # TODO: No thumbnails, has waveforms
    return media_data.get('images', {}).get('waveform_m', None)


def _get_creator_data(item):
    creator = item.get('username').strip()
    if creator:
        creator_url = f"https://freesound.org/people/{creator}/"
    else:
        creator_url = None
    return creator, creator_url


def _get_title(item):
    title = item.get('name')
    return title


def _get_metadata(item):
    metadata = {}
    fields = ['description', 'num_downloads', 'avg_rating', 'num_rating']
    for field in fields:
        field_value = item.get(field)
        if field_value:
            metadata[field] = field_value
    return metadata


def _get_tags(item):
    return item.get('tags')


def _get_license(item):
    item_license_url = item.get('license')
    item_license = get_license_info(license_url=item_license_url)

    if item_license.license is None:
        return None
    return item_license


def _cleanse_url(url_string):
    """
    Check to make sure that a url is valid, and prepend a protocol if needed
    """

    parse_result = urlparse(url_string)

    if parse_result.netloc == HOST:
        parse_result = urlparse(url_string, scheme='https')
    elif not parse_result.scheme:
        parse_result = urlparse(url_string, scheme='http')

    if parse_result.netloc or parse_result.path:
        return parse_result.geturl()


if __name__ == '__main__':
    main()
