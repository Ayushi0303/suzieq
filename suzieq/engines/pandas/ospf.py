from ipaddress import IPv4Network
import pandas as pd

from suzieq.sqobjects.lldp import LldpObj
from suzieq.engines.pandas.engineobj import SqEngineObject


class OspfObj(SqEngineObject):

    def _get_combined_df(self, **kwargs):
        """OSPF has info divided across multiple tables. Get a single one"""

        if self.ctxt.sort_fields is None:
            sort_fields = None
        else:
            sort_fields = self.sort_fields

        columns = kwargs.get('columns', ['default'])
        state = kwargs.pop('state', '')
        addnl_fields = kwargs.pop('addnl_fields', self.iobj._addnl_fields)
        addnl_nbr_fields = self.iobj._addnl_nbr_fields

        if state == "pass":
            query_str = 'adjState == "full" or adjState == "passive"'
        elif state == "fail":
            query_str = 'adjState != "full" and adjState != "passive"'
        else:
            query_str = ''

        df = self.get_valid_df('ospfIf', sort_fields,
                               addnl_fields=addnl_fields, **kwargs)
        nbr_df = self.get_valid_df('ospfNbr', sort_fields,
                                   addnl_fields=addnl_nbr_fields, **kwargs)
        if nbr_df.empty:
            return nbr_df

        # Merge the two tables
        df = df.merge(nbr_df, on=['namespace', 'hostname', 'ifname'],
                      how='left')

        if columns == ['*']:
            df = df.drop(columns=['area_y', 'instance_y', 'vrf_y',
                                  'areaStub_y', 'timestamp_y']) \
                .rename(columns={
                    'instance_x': 'instance', 'areaStub_x': 'areaStub',
                    'area_x': 'area', 'vrf_x': 'vrf',
                    'state_x': 'ifState', 'state_y': 'adjState',
                    'timestamp_x': 'timestamp'})
        else:
            df = df.rename(columns={'vrf_x': 'vrf', 'area_x': 'area',
                                    'state_x': 'ifState', 'state_y': 'adjState',
                                    'timestamp_x': 'timestamp'})
            df = df.drop(list(df.filter(regex='_y$')), axis=1) \
                .fillna({'peerIP': '-', 'numChanges': 0,
                         'lastChangeTime': 0})

        # Fill the adjState column with passive if passive
        if 'passive' in df.columns:
            df.loc[df['adjState'].isnull(), 'adjState'] = df['passive']
            df.loc[df['adjState'].eq(True), 'adjState'] = 'passive'
            df.loc[df['adjState'].eq(False), 'adjState'] = 'fail'
            df.drop(columns=['passive'], inplace=True)

        df.bfill(axis=0, inplace=True)

        # Move the timestamp column to the end
        cols = df.columns.to_list()
        cols.remove('timestamp')
        cols.append('timestamp')
        if query_str:
            return df[cols].query(query_str)
        return df[cols]

    def get(self, **kwargs):
        return self._get_combined_df(**kwargs)

    def summarize(self, **kwargs):
        """Describe the data"""

        # Discard these
        kwargs.pop('columns', None)

        self._init_summarize('ospfIf', **kwargs)
        if self.summary_df.empty:
            return self.summary_df

        self._summarize_on_add_field = [
            ('deviceCnt', 'hostname', 'nunique'),
            ('peerCnt', 'hostname', 'count'),
        ]

        self._summarize_on_add_with_query = [
            ('stubbyPeerCnt', 'areaStub', 'areaStub'),
            ('passivePeerCnt', 'adjState == "passive"', 'ifname'),
            ('unnumberedPeerCnt', 'isUnnumbered', 'isUnnumbered'),
            ('failedPeerCnt', 'adjState != "passive" and nbrCount == 0',
             'ifname'),
        ]

        self._summarize_on_add_list_or_count = [
            ('area', 'area'),
            ('vrf', 'vrf'),
            ('helloTime', 'helloTime'),
            ('deadTime', 'deadTime'),
            ('retxTime', 'retxTime'),
            ('networkType', 'networkType'),
        ]

        self.summary_df['lastChangeTime'] = pd.to_datetime(
            self.summary_df['lastChangeTime'], unit='ms', errors='ignore')
        self.summary_df['lastChangeTime'] = (
            self.summary_df['timestamp'] - self.summary_df['lastChangeTime'])
        self.summary_df['lastChangeTime'] = self.summary_df['lastChangeTime'] \
                                                .apply(lambda x: x.round('s'))

        self._summarize_on_add_stat = [
            ('adjChangesStat', '', 'numChanges'),
            ('upTimeStat', 'adjState == "full"', 'lastChangeTime'),
        ]

        self._gen_summarize_data()
        self._post_summarize()
        return self.ns_df.convert_dtypes()

    def unique(self, **kwargs):
        return super().unique(**kwargs)

    def aver(self, **kwargs):
        """Assert that the OSPF state is OK"""

        columns = [
            "namespace",
            "hostname",
            "vrf",
            "ifname",
            "routerId",
            "helloTime",
            "deadTime",
            "passive",
            "ipAddress",
            "isUnnumbered",
            "areaStub",
            "networkType",
            "timestamp",
            "area",
            "nbrCount",
        ]
        sort_fields = ["namespace", "hostname", "ifname", "vrf"]

        ospf_df = self.get_valid_df(
            "ospfIf", sort_fields, columns=columns, **kwargs)
        if ospf_df.empty:
            return pd.DataFrame(columns=columns)

        ospf_df["assertReason"] = [[] for _ in range(len(ospf_df))]
        df = (
            ospf_df[ospf_df["routerId"] != ""]
            .groupby(["routerId", "namespace"], as_index=False)[["hostname",
                                                                 "namespace"]]
            .agg(lambda x: x.unique().tolist())
        ).dropna(how='any')

        # df is a dataframe with each row containing the routerId and the
        # corresponding list of hostnames with that routerId. In a good
        # configuration, the list must have exactly one entry
        ospf_df['assertReason'] = (
            ospf_df.merge(df, on=["routerId"], how="outer")
            .apply(lambda x: ["duplicate routerId {}".format(
                x["hostname_y"])]
                if len(x['hostname_y']) != 1 else [], axis=1))

        # Now  peering match
        lldpobj = LldpObj(context=self.ctxt)
        lldp_df = lldpobj.get(
            namespace=kwargs.get("namespace", ""),
            hostname=kwargs.get("hostname", ""),
            ifname=kwargs.get("ifname", ""),
            columns=["namespace", "hostname", "ifname", "peerHostname",
                     "peerIfname", "peerMacaddr"]
        )
        if lldp_df.empty:
            ospf_df = ospf_df[~(ospf_df.ifname.str.contains('loopback') |
                                ospf_df.ifname.str.contains('Vlan'))]
            ospf_df['assertReason'] = 'No LLDP peering info'
            ospf_df['assert'] = 'fail'
            return ospf_df[['namespace', 'hostname', 'vrf', 'ifname',
                            'assertReason', 'assert']]

        # Create a single massive DF with fields populated appropriately
        use_cols = [
            "namespace",
            "routerId",
            "hostname",
            "vrf",
            "ifname",
            "helloTime",
            "deadTime",
            "passive",
            "ipAddress",
            "areaStub",
            "isUnnumbered",
            "networkType",
            "area",
            "timestamp",
        ]

        int_df = ospf_df[use_cols].merge(lldp_df,
                                         on=["namespace", "hostname",
                                             "ifname"]) \
            .dropna(how="any")

        if int_df.empty:
            # Weed out the loopback and SVI interfaces as they have no LLDP peers
            ospf_df = ospf_df[~(ospf_df.ifname.str.contains('loopback') |
                                ospf_df.ifname.str.contains('Vlan'))]
            ospf_df['assertReason'] = 'No LLDP peering info'
            ospf_df['assert'] = 'fail'
            return ospf_df[['namespace', 'hostname', 'vrf', 'ifname',
                            'assertReason', 'assert']]

        ospf_df = ospf_df.merge(int_df,
                                left_on=["namespace", "hostname", "ifname"],
                                right_on=["namespace", "peerHostname",
                                          "peerIfname"]) \
            .dropna(how="any")

        # Now start comparing the various parameters
        ospf_df["assertReason"] += ospf_df.apply(
            lambda x: ["subnet mismatch"]
            if (
                (x["isUnnumbered_x"] != x["isUnnumbered_y"])
                and (
                    IPv4Network(x["ipAddress_x"], strict=False)
                    != IPv4Network(x["ipAddress_y"], strict=False)
                )
            )
            else [],
            axis=1,
        )
        ospf_df["assertReason"] += ospf_df.apply(
            lambda x: ["area mismatch"]
            if (x["area_x"] != x["area_y"] and x["areaStub_x"] != x["areaStub_y"])
            else [],
            axis=1,
        )
        ospf_df["assertReason"] += ospf_df.apply(
            lambda x: ["Hello timers mismatch"]
            if x["helloTime_x"] != x["helloTime_y"]
            else [],
            axis=1,
        )
        ospf_df["assertReason"] += ospf_df.apply(
            lambda x: ["Dead timer mismatch"]
            if x["deadTime_x"] != x["deadTime_y"]
            else [],
            axis=1,
        )
        ospf_df["assertReason"] += ospf_df.apply(
            lambda x: ["network type mismatch"]
            if x["networkType_x"] != x["networkType_y"]
            else [],
            axis=1,
        )
        ospf_df["assertReason"] += ospf_df.apply(
            lambda x: ["passive config mismatch"]
            if x["passive_x"] != x["passive_y"]
            else [],
            axis=1,
        )
        ospf_df["assertReason"] += ospf_df.apply(
            lambda x: ["vrf mismatch"] if x["vrf_x"] != x["vrf_y"] else [],
            axis=1,
        )

        # Fill up a single assert column now indicating pass/fail
        ospf_df['assert'] = ospf_df.apply(lambda x: 'pass'
                                          if not len(x['assertReason'])
                                          else 'fail', axis=1)

        return (
            ospf_df.rename(
                index=str,
                columns={
                    "hostname_x": "hostname",
                    "ifname_x": "ifname",
                    "vrf_x": "vrf",
                },
            )[["namespace", "hostname", "ifname", "vrf", "assert",
               "assertReason", "timestamp"]].explode(column='assertReason')
            .fillna({'assertReason': '-'})
        )
