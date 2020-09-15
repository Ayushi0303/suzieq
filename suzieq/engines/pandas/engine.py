import os
import logging
from pathlib import Path
from importlib import import_module
import pyarrow.dataset as ds

import pandas as pd
import pyarrow.parquet as pa

from suzieq.engines.base_engine import SqEngine


class SqPandasEngine(SqEngine):
    def __init__(self):
        self.logger = logging.getLogger()

    def get_table_df(self, cfg, **kwargs) -> pd.DataFrame:
        """Use Pandas instead of Spark to retrieve the data"""

        self.cfg = cfg

        table = kwargs.pop("table")
        start = kwargs.pop("start_time")
        end = kwargs.pop("end_time")
        view = kwargs.pop("view")
        fields = kwargs.pop("columns")
        addnl_filter = kwargs.pop("add_filter", None)
        key_fields = kwargs.pop("key_fields")

        folder = self._get_table_directory(table)

        if addnl_filter:
            # This is for special cases that are specific to an object
            query_str = addnl_filter
        else:
            query_str = None

        if query_str is None:
            # Make up a dummy query string to avoid if/then/else
            query_str = "timestamp != 0"

        try:
            dirs = Path(folder)
            datasets = []
            for elem in dirs.iterdir():
                if 'sqvers=' not in str(elem):
                    continue
                datasets.append(ds.dataset(elem, format='parquet',
                                           partitioning='hive'))

            if not datasets:
                datasets = [ds.dataset(folder, format='parquet',
                                       partitioning='hive')]

            # Build the filters for predicate pushdown
            master_schema = self._build_master_schema(datasets)

            filters = self.build_ds_filters(
                start, end, master_schema, **kwargs)

            final_df = ds.dataset(datasets) \
                         .to_table(filter=filters, columns=fields) \
                         .to_pandas(self_destruct=True) \
                         .query(query_str)

            if (not final_df.empty and (view == 'latest') and
                    all(x in final_df.columns for x in key_fields)):
                final_df = final_df.set_index(key_fields) \
                    .query('~index.duplicated(keep="last")') \
                    .query('active==True') \
                    .reset_index()
            elif not final_df.empty:
                final_df = final_df.query('active == True') \
                                   .reset_index()
        except (pa.lib.ArrowInvalid, OSError):
            return pd.DataFrame(columns=fields)

        fields = [x for x in fields if x in final_df.columns]

        return final_df[fields]

    def _build_master_schema(self, datasets: list) -> pa.lib.Schema:
        """Build the master schema from the list of diff versions
        We use this to build the filters and use the right type-based check
        for a field.
        """
        msch = datasets[0].schema
        msch_set = set(msch)
        for dataset in datasets[1:]:
            sch = dataset.schema
            sch_set = set(sch)
            if msch_set.issuperset(sch):
                continue
            elif sch_set.issuperset(msch):
                msch = sch
            else:
                for fld in sch_set-msch_set:
                    index = sch.get_field_index(fld.name)
                    msch.insert(index, fld)

        return msch

    def get_object(self, objname: str, iobj):
        module = import_module("suzieq.engines.pandas." + objname)
        eobj = getattr(module, "{}Obj".format(objname.title()))
        return eobj(iobj)

    def build_ds_filters(self, start_tm: str, end_tm: str,
                         schema: pa.lib.Schema,
                         **kwargs) -> ds.Expression:
        """The new style of filters using dataset instead of ParquetDataset"""

        # The time filters first
        timeset = []
        if start_tm and not end_tm:
            timeset = pd.date_range(
                start=pd.to_datetime(start_tm, infer_datetime_format=True),
                periods=2,
                freq="15min",
            )
            filters = ds.field("timestamp") >= (timeset[0].timestamp() * 1000)
        elif end_tm and not start_tm:
            timeset = pd.date_range(
                end=pd.to_datetime(
                    end_tm, infer_datetime_format=True),
                periods=2,
                freq="15min",
            )
            filters = ds.field("timestamp") <= (timeset[-1].timestamp() * 1000)
        elif start_tm and end_tm:
            timeset = [
                pd.to_datetime(start_tm, infer_datetime_format=True),
                pd.to_datetime(end_tm, infer_datetime_format=True),
            ]
            filters = ((ds.field("timestamp") >= (timeset[0].timestamp() * 1000))
                       & (ds.field("timestamp") <= (timeset[-1].timestamp() * 1000)))
        else:
            filters = (ds.field("timestamp") != 0)

        sch_fields = schema.names
        for k, v in kwargs.items():
            if not v:
                continue
            if k not in sch_fields:
                self.logger.warning(f'Ignoring invalid field {k} in filter')
                continue

            ftype = schema.field(k).type
            if isinstance(v, list):
                infld = []
                notinfld = []
                for e in v:
                    if isinstance(e, str) and e.startswith("!"):
                        if ftype == 'int64':
                            notinfld.append(int(e[1:]))
                        else:
                            notinfld.append(e[1:])
                    else:
                        if ftype == 'int64':
                            infld.append(int(e))
                        else:
                            infld.append(e)
                if infld and notinfld:
                    filters = filters & (ds.field(k).isin(infld) &
                                         ~ds.field(k).isin(notinfld))
                elif infld:
                    filters = filters & (ds.field(k).isin(infld))
                elif notinfld:
                    filters = filters & (~ds.field(k).isin(notinfld))
            else:
                if isinstance(v, str) and v.startswith("!"):
                    if ftype == 'int64':
                        filters = filters & (ds.field(k) != int(v[1:]))
                    else:
                        filters = filters & (ds.field(k) != v[1:])
                else:
                    if ftype == 'int64':
                        filters = filters & (ds.field(k) == int(v))
                    else:
                        filters = filters & (ds.field(k) == v)

        return filters

    def _get_table_directory(self, table):
        assert table
        folder = "{}/{}".format(self.cfg.get("data-directory"), table)
        # print(f"FOLDER: {folder}", file=sys.stderr)
        return folder

    def get_tables(self, cfg, **kwargs):
        """finds the tables that are available"""
        if not getattr(self, 'cfg', None):
            self.cfg = cfg
        dfolder = self.cfg['data-directory']

        tables = []
        if dfolder:
            p = Path(dfolder)
            tables = [dir.parts[-1] for dir in p.iterdir()
                      if dir.is_dir() and not dir.parts[-1].startswith('_')]
            namespaces = kwargs.get('namespace', [])
            for dc in namespaces:
                tables = list(filter(
                    lambda x: os.path.exists('{}/{}/namespace={}'.format(
                        dfolder, x, dc)), tables))

        return tables
