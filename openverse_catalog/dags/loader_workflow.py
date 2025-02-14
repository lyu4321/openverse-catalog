"""\
#### Database Loader DAG
**DB Loader Apache Airflow DAG** (directed acyclic graph) takes the media data saved
locally in TSV files, cleans it using an intermediate database table, and saves
the cleaned-up data into the main database (also called upstream or Openledger).

In production,"locally" means on AWS EC2 instance that runs the Apache Airflow
webserver. Storing too much data there is dangerous, because if ingestion to the
database breaks down, the disk of this server gets full, and breaks all
Apache Airflow operations.

As a first step, the DB Loader Apache Airflow DAG saves the data gathered by
Provider API Scripts to S3 before attempting to load it to PostgreSQL, and delete
 it from disk if saving to S3 succeeds, even if loading to PostgreSQL fails.

This way, we can delete data from the EC2 instance to open up disk space without
 the possibility of losing that data altogether. This will allow us to recover if
 we lose data from the DB somehow, because it will all be living in S3.
It's also a prerequisite to the long-term plan of saving data only to S3
(since saving it to the EC2 disk is a source of concern in the first place).

This is one step along the path to avoiding saving data on the local disk at all.
It should also be faster to load into the DB from S3, since AWS RDS instances
provide special optimized functionality to load data from S3 into tables in the DB.

Loading the data into the Database is a two-step process: first, data is saved
to the intermediate table. Any items that don't have the required fields
(media url, license, foreign landing url and foreign id), and duplicates as
determined by combination of provider and foreign_id are deleted.
Then the data from the intermediate table is upserted into the main database.
If the same item is already present in the database, we update its information
with newest (non-null) data, and merge any metadata or tags objects to preserve all
previously downloaded data, and update any data that needs updating
(eg. popularity metrics).

You can find more background information on the loading process in the following
issues and related PRs:

- [[Feature] More sophisticated merging of columns in PostgreSQL when upserting](
https://github.com/creativecommons/cccatalog/issues/378)

- [DB Loader DAG should write to S3 as well as PostgreSQL](
https://github.com/creativecommons/cccatalog/issues/333)

- [DB Loader should take data from S3, rather than EC2 to load into PostgreSQL](
https://github.com/creativecommons/cccatalog/issues/334)

"""
import logging
import os
from datetime import datetime, timedelta

from airflow import DAG
from util.loader import operators
from util.pg_cleaner import OVERWRITE_DIR


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s:  %(message)s", level=logging.INFO
)

logger = logging.getLogger(__name__)

DAG_ID = "tsv_to_postgres_loader"
OVERWRITE_DAG_ID = "tsv_to_postgres_loader_overwrite"
DB_CONN_ID = os.getenv("OPENLEDGER_CONN_ID", "postgres_openledger_testing")
AWS_CONN_ID = os.getenv("AWS_CONN_ID", "no_aws_conn_id")
CCCATALOG_STORAGE_BUCKET = os.getenv("CCCATALOG_STORAGE_BUCKET")
MINIMUM_FILE_AGE_MINUTES = int(os.getenv("LOADER_FILE_AGE", 15))
CONCURRENCY = 5
SCHEDULE_CRON = "* * * * *"
TIMESTAMP_TEMPLATE = "{{ ts_nodash }}"

OUTPUT_DIR_PATH = os.path.realpath(os.getenv("OUTPUT_DIR", "/tmp/"))

DAG_DEFAULT_ARGS = {
    "owner": "data-eng-admin",
    "depends_on_past": False,
    "start_date": datetime(2020, 1, 15),
    "email_on_retry": False,
    "retries": 2,
    "retry_delay": timedelta(seconds=15),
}


def create_dag(
    dag_id=DAG_ID,
    args=DAG_DEFAULT_ARGS,
    concurrency=CONCURRENCY,
    max_active_runs=CONCURRENCY,
    schedule_cron=SCHEDULE_CRON,
    postgres_conn_id=DB_CONN_ID,
    aws_conn_id=AWS_CONN_ID,
    output_dir=OUTPUT_DIR_PATH,
    storage_bucket=CCCATALOG_STORAGE_BUCKET,
    minimum_file_age_minutes=MINIMUM_FILE_AGE_MINUTES,
    overwrite=False,
):
    dag = DAG(
        dag_id=dag_id,
        default_args=args,
        concurrency=concurrency,
        max_active_runs=max_active_runs,
        schedule_interval=schedule_cron,
        catchup=False,
    )
    if overwrite is True:
        identifier = "overwrite" + TIMESTAMP_TEMPLATE
    else:
        identifier = TIMESTAMP_TEMPLATE

    with dag:
        stage_oldest_tsv_file = operators.get_file_staging_operator(
            dag,
            output_dir,
            minimum_file_age_minutes,
            identifier=identifier,
        )
        create_loading_table = operators.get_table_creator_operator(
            dag,
            postgres_conn_id,
            identifier=identifier,
        )
        copy_to_s3 = operators.get_copy_to_s3_operator(
            dag,
            output_dir,
            storage_bucket,
            aws_conn_id,
            identifier=identifier,
        )
        load_s3_data = operators.get_load_s3_data_operator(
            dag,
            storage_bucket,
            aws_conn_id,
            postgres_conn_id,
            overwrite=overwrite,
            identifier=identifier,
        )
        one_failed_s3 = operators.get_one_failed_switch(dag, "s3")
        load_local_data = operators.get_load_local_data_operator(
            dag,
            output_dir,
            postgres_conn_id,
            overwrite=overwrite,
            identifier=identifier,
        )
        one_success_save = operators.get_one_success_switch(dag, "save")
        all_done_save = operators.get_all_done_switch(dag, "save")
        all_failed_save = operators.get_all_failed_switch(dag, "save")
        delete_staged_file = operators.get_file_deletion_operator(
            dag,
            output_dir,
            identifier=identifier,
        )
        one_failed_delete = operators.get_one_failed_switch(dag, "delete")
        drop_loading_table = operators.get_drop_table_operator(
            dag,
            postgres_conn_id,
            identifier=identifier,
        )
        move_staged_failures = operators.get_failure_moving_operator(
            dag,
            output_dir,
            identifier=identifier,
        )
        (stage_oldest_tsv_file >> [create_loading_table, copy_to_s3] >> load_s3_data)
        [copy_to_s3, load_s3_data] >> one_failed_s3
        [create_loading_table, one_failed_s3] >> load_local_data
        [copy_to_s3, load_local_data] >> one_success_save
        [copy_to_s3, load_local_data] >> all_done_save
        [copy_to_s3, load_local_data] >> all_failed_save
        [one_success_save, all_done_save] >> delete_staged_file
        [load_s3_data, load_local_data] >> drop_loading_table
        delete_staged_file >> one_failed_delete
        [one_failed_delete, all_failed_save] >> move_staged_failures
    return dag


globals()[DAG_ID] = create_dag()
globals()[OVERWRITE_DAG_ID] = create_dag(
    dag_id=OVERWRITE_DAG_ID,
    output_dir=os.path.join(OUTPUT_DIR_PATH, OVERWRITE_DIR),
    overwrite=True,
)
