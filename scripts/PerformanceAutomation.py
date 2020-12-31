import json
import subprocess
from datetime import datetime, timedelta, date
import psycopg2
from os import path,  devnull
import schedule
import time
import sys
import logging

configfile = sys.argv[1]
build_no = sys.argv[2]
if len(sys.argv) > 3:
    start_time = sys.argv[3]
else:
    start_time = "00:00"


def subprocess_cmd(command):
    process = subprocess.Popen(command,stdout=subprocess.PIPE, shell=True)
    proc_stdout = process.communicate()[0].strip()
    return proc_stdout


def create_conn():
    try:
        db_conn = psycopg2.connect(host=db_host, port=db_port, user=db_user, database=database)
        db_conn.autocommit = True
        cursor = db_conn.cursor()
        return cursor
    except Exception as err:
        logging.error("Cannot connect to database")
        return


def copy_to_slave():
    with open(devnull, 'w') as FNULL:
        remote_user = jmeter_config['JmeterSetting']['SplitMode']['remoteUser']
        for slave in slave_list:
            logging.info(f'slave : {slave}')
            copy_config = f'scp {configfile} {remote_user}@{slave}:{path.join(project_path, "config/")}'
            subprocess.Popen(copy_config, shell=True).wait()
            copy_python = f'scp {path.join(project_path, "scripts/", path.basename(__file__))} {remote_user}@{slave}:{path.join(project_path, "scripts/")}'
            subprocess.Popen(copy_python, shell=True).wait()
            remote_invoke = f'ssh -C {remote_user}@{slave} python3 {path.join(project_path, "scripts/", path.basename(__file__))} {configfile} {build_no} {start_time} '
            logging.info(f'Starting Load test Automation on {slave} and command is : {remote_invoke}')
            subprocess.Popen(remote_invoke, shell=True, stdout=FNULL)


def housekeeping():
    hk_query = "select table_name from (WITH CTE AS " + \
               "( SELECT " + \
               "        table_name, " + \
               "                (SELECT pg_relation_filepath(table_name)) path " + \
               "    FROM information_schema.tables " + \
               "    WHERE table_type = 'BASE TABLE' " + \
               "    AND table_schema = 'public' " + \
               "        and table_name ilike 'table%' ) " + \
               "SELECT " + \
               "    table_name " + \
               "    ,( SELECT access " + \
               "        FROM pg_stat_file(path) " + \
               "    ) as creation_time " + \
               "FROM CTE) as temp " + \
               "where creation_time <  NOW() - INTERVAL '" + jmeter_config['housekeeping'][
                   'rawTableRetention'] + " day';"

    cur = create_conn()
    cur.execute(hk_query)
    table_list = cur.fetchall()
    for tables in table_list:
        cur.execute(" DROP table \"" + tables[0] + "\";")
        logging.info(f"Drop table {tables[0]} Older than {jmeter_config['housekeeping']['rawTableRetention']} days")
    cur.close()
    backup_files = f'find {path.join(project_path, "results/")} -maxdepth 1 -type f \( -name \"*.xml\" -o -name \"*.csv\" \)' \
                   f' -mtime +{jmeter_config["housekeeping"]["movetoBackup"]} -exec mv {{}} {path.join(project_path, "results/backup/")} \;'
    backup_folder = f'find {path.join(project_path, "results/")} -maxdepth 1 -type d \( ! -name backup \) -mtime ' \
                    f'+{jmeter_config["housekeeping"]["movetoBackup"]} -exec mv {{}} {path.join(project_path, "results/backup/")} \;'
    remove_old = "find " + path.join(project_path, "results/backup/") + \
                 " -maxdepth 1  -mtime +" + jmeter_config['housekeeping']['deleteFromBackup'] + " -exec rm -r {} \;"
    remove_old_logs = "find " + path.join(project_path, "logs/backup/") + \
                 " -maxdepth 1  \( ! -name scripts \) -mtime +" + jmeter_config['housekeeping'][
                     'deleteFromBackup'] + " -exec rm -r {} \;"
    subprocess.Popen(backup_files, shell=True).wait()
    subprocess.Popen(backup_folder, shell=True).wait()
    subprocess.Popen(remove_old, shell=True).wait()
    subprocess.Popen(remove_old_logs, shell=True).wait()
    freespace = subprocess.check_output("df -h . | grep -v Size | awk '{print $4}'", shell=True).decode().strip()
    logging.info(f"Backup files and folder completed. Available space is {freespace}")


def start_hardware_stats(hw_time):
    ms_ips_path = str(jmeter_config['HardwareStats']['ms_ips_path'])
    ms_user = str(jmeter_config['HardwareStats']['user']).strip()
    # duration = str(jmeter_config['HardwareStats']['duration']).strip()
    script = str(jmeter_config['HardwareStats']['script'])
    with open(f"{ms_ips_path}", "r") as IP_list:
        for ip in IP_list:
            # sshProcess = subprocess.Popen(['ssh','perfuser@{}'.format(ip)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,universal_newlines=True,bufsize=0)

            sshProcess = subprocess.Popen(['ssh', '{}@{}'.format(ms_user,ip)], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, universal_newlines=True, bufsize=0)
            sshProcess.stdin.write(f"python {script} {total_run_time} {hw_time} {is_split_mode}")
            sshProcess.stdin.close()
            logging.info(f'Started hardware data collection on {ip}')


def cloudwatch_metrics():
    ms_cms_service = str(jmeter_config['HardwareStats']['ms_cms_service']).strip()
    kafka_cms_service = str(jmeter_config['HardwareStats']['kafka_cms_service']).strip()
    cassandra_cms_service = str(jmeter_config['HardwareStats']['cassandra_cms_service']).strip()
    zookeeper_cms_service = str(jmeter_config['HardwareStats']['zookeeper_cms_service']).strip()
    logging.info('Executing cloudwatch data collection script')
    # duration = str(jmeter_config['HardwareStats']['duration']).strip()
    parameters = f"{ms_cms_service} {kafka_cms_service} {cassandra_cms_service} {zookeeper_cms_service} {test_report_path}/hardwareUtilization {total_run_time}"
    subprocess_cmd([f"python {project_path}/scripts/p90_cw_stats.py {ms_cms_service} {kafka_cms_service} {cassandra_cms_service} {zookeeper_cms_service} {test_report_path}/hardwareUtilization {total_run_time}"])


def execute_test(lt_command, name):
    if name == 'SanityTest':
        logging.info("Starting the Sanity test....")
        global sanity_status
        sanity_status = 0
        try:
            sanity_process = subprocess.Popen(lt_command, shell=True)
            sanity_process.wait(sanityTestDuration)
        except subprocess.TimeoutExpired:
            stop_sanity = f'sh {path.join(JMETER_HOME, "bin/stoptest.sh")}'
            subprocess.call(stop_sanity, shell=True)
            logging.info("Finished the Sanity test....")
        schedule.CancelJob()
    else:
        logging.info("Starting the Load test....")
        global run_status
        run_status = 0
        test_start_time = datetime.now()
        subprocess.Popen(lt_command, shell=True).wait()
        test_end_time = datetime.now()
        # global test_duration
        # test_duration = float((test_end_time - test_start_time).seconds)
        logging.info("Finished the Load test....")
        schedule.CancelJob()


def upload_jtl():
    logging.info("Report processing started ...")
    with open(jtl_file) as jfileobject:
        table_header = jfileobject.readline()
        table_header = table_header.replace(",", " text, ") + " text"
        table_header = table_header.replace("timeStamp text", "timeStamp double precision")
        table_header = table_header.replace("label text", "API_name text")
        table_header = table_header.replace("elapsed text", "elapsed integer")
        table_header = table_header.replace("IdleTime text", "IdleTime integer")
        table_header = table_header.replace("Latency text", "Latency integer")
        table_header = table_header.replace("Connect text", "Connect integer")
        table_header = table_header.replace("bytes text", "bytes integer")
        table_header = table_header.replace("sentBytes text", "sentbytes integer")
        logging.info(table_header)
        cur = create_conn()
        try:
            cur.execute("create table if not exists table_" + report_file + " ( " + table_header + " );")
            cur.copy_from(jfileobject, "table_" + report_file, sep=',')
            logging.info(f'Uploadted JTL file {jtl_file} to table : table_{report_file}')
        except Exception as err:
            logging.error(f"Error while creating table : {err}")
        cur.close()


def process_report():
    time.sleep(60)
    cur = create_conn()
    alter_table = f'ALTER TABLE table_{report_file} ALTER timestamp TYPE timestamp without time zone  ' \
                  f'USING to_timestamp(timestamp/1000) ;'
    try:
        cur.execute(alter_table)
    except Exception as err:
        logging.info(f'Error while altering the table {err}')
        time.sleep(300)
        cur.execute(alter_table)
    logging.info(f'Altered the table table_{report_file}')
    utc_time = datetime.strptime(test_time, "%d%b%Y_%H%M%S")
    utc_time = utc_time.strftime("%Y-%m-%d %X")
    query = f"select " \
            f"api_name, samples, min, Average, max, p70tile, p80tile, p90tile,  " \
            f"(errors/samples::float)*100 as perc_error, " \
            f"ROUND((samples/{peak_duration}),1) as throughput, " \
            f"received_kbsec, send_kbsec " \
            f"from(   " \
            f"    select api_name, " \
            f"    count(*) as samples, " \
            f"    min(elapsed) as min, " \
            f"    ROUND(avg(elapsed), 1)  Average, " \
            f"    percentile_cont(0.7) within group (order by elapsed) as p70tile, " \
            f"    percentile_cont(0.8) within group (order by elapsed) as p80tile, " \
            f"    percentile_cont(0.9) within group (order by elapsed) as p90tile, " \
            f"    max(elapsed) as max, " \
            f"    ROUND(sum(bytes)/(1024.0 * {peak_duration}),1) received_kbsec, " \
            f"    ROUND(sum(sentbytes)/(1024.0 * {peak_duration}),1) send_kbsec, " \
            f"    sum(case when success = 'false' then 1 else 0 end) errors " \
            f"    from table_{report_file} where timestamp >= '{peak_start_time}' AND timestamp <= '{peak_end_time}'" \
            f"group by api_name)x " \
            f"order by api_name"

    cur = create_conn()
    test_report = "COPY ({0}) TO STDOUT WITH CSV HEADER".format(query)
    with open(csv_file, 'w') as csv_f:
        cur.copy_expert(test_report, csv_f)
    logging.info(f'Report generated for {peak_duration} from {peak_start_time} to {peak_end_time}')
    logging.info(f'Final report {csv_file}')
    uploadResultInDB = str(jmeter_config['JmeterSetting']['mainSetting']['uploadResultInDB']).lower()
    if uploadResultInDB == "true" :
        upload_query = query.replace("received_kbsec, send_kbsec",
                                     f" received_kbsec, send_kbsec, '{utc_time}' as datetime, '{build_no}' " \
                                     f" as Build_number, 'table_{report_file}' as rawtable ")
        db_report = "COPY ({0}) TO STDOUT WITH CSV HEADER".format(upload_query)
        with open(db_csv_file, 'w') as csv_f:
            cur.copy_expert(db_report, csv_f)
        with open(db_csv_file, 'r') as csv_f:
            next(csv_f)
            cur.copy_from(csv_f, micro_service, sep=',')
        logging.info(f'Results are uploaded to main table')
    cur.close()


def get_hardware_stats():
    ms_ips_path = str(jmeter_config['HardwareStats']['ms_ips_path'])
    ms_user = str(jmeter_config['HardwareStats']['user']).strip()
    #test_report_path = path.join(project_path, "results", report_file)
    hardware_stat_path = str(jmeter_config['HardwareStats']['hardware_stat_path']).strip()
    hw_dir = f'mkdir -p -m 755 {path.join(test_report_path, "hardwareUtilization")}'
    subprocess.call(hw_dir, shell=True)
    #with open(devnull, 'w') as FNULL:
    with open(f"{ms_ips_path}", "r") as IP_list:
        for ip in IP_list:
            file_exist = False

            while not file_exist:
                stat_cmd = f'ssh {ms_user}@{ip} stat /home/perfuser/load-test-scripts/CPUUSAGE*'
                try:
                    stat_result = subprocess.run(["scp",f'{ms_user}@{ip}:/home/perfuser/load-test-scripts/CPUUSAGE*',f'{path.join(test_report_path, "hardwareUtilization/")}'])
                    if stat_result.returncode == 0:
                        subprocess.run(["scp",f'{ms_user}@{ip}:/home/perfuser/load-test-scripts/CPUUSAGE*',f'{path.join(test_report_path, "hardwareUtilization/")}'])
                        logging.info(f'Hardware util file exists on {ip}, copying to local')
                        file_exist = True
                    else:
                        time.sleep(20)
                        
                    
                except Exception:
                    logging.info(f'File /home/perfuser/load-test-scripts/CPUUSAGE_{ip}.txt not found on {ip}')
                    time.sleep(10)


def upload_hardware_data_psql():
    copy_statement = "copy hardware_utilization(hostname,available_mem,cpu_max,cpu_avg,cpu_90th_per,raw_table,build_number,service_name,datetime) from STDIN DELIMITER ',' CSV HEADER"
    cur = create_conn()
    cpu_usage_file = open(f'{test_report_path}/hardwareUtilization/CPUUSAGE.txt', 'r')
    logging.info('Uploading hardware data into PostgreSQL table hardware_utilization')
    cur.copy_expert(copy_statement,cpu_usage_file)
    cpu_usage_file.close()
    cur.close()	


def consolidate_hw_files():
    subprocess_cmd("""
    awk 'FNR>1 || NR==1' """+test_report_path+"""/hardwareUtilization/CPUUSAGE_*.txt > """+test_report_path+"""/hardwareUtilization/CPUUSAGE.txt
    cp """+test_report_path+"""/hardwareUtilization/CPUUSAGE.txt """+test_report_path+"""/hardwareUtilization/CPUUSAGE_org.txt
    """)
    subprocess_cmd("""
    awk -v d="""+report_file+""" -F"," 'BEGIN { OFS = "," } {$6=d; print}' """+test_report_path+"""/hardwareUtilization/CPUUSAGE.txt > """+test_report_path+"""/hardwareUtilization/CPUUSAGE_1.txt
    """)
    subprocess_cmd("""
    rm -rf """+test_report_path+"""/hardwareUtilization/CPUUSAGE.txt
    """)
    subprocess_cmd("""
    awk -v d="""+build_no+""" -F"," 'BEGIN { OFS = "," } {$7=d; print}'  """+test_report_path+"""/hardwareUtilization/CPUUSAGE_1.txt > """+test_report_path+"""/hardwareUtilization/CPUUSAGE.txt
    """)
    subprocess_cmd("""
    rm -rf """+test_report_path+"""/hardwareUtilization/CPUUSAGE_1.txt
    """)
    subprocess_cmd("""
    awk -v d="""+micro_service+""" -F"," 'BEGIN { OFS = "," } {$8=d; print}'  """+test_report_path+"""/hardwareUtilization/CPUUSAGE.txt > """+test_report_path+"""/hardwareUtilization/CPUUSAGE_1.txt""")

    subprocess_cmd("""
    rm -rf """+test_report_path+"""/hardwareUtilization/CPUUSAGE.txt
    """)
    subprocess_cmd("""
    awk -v d="""+test_time+""" -F"," 'BEGIN { OFS = "," } {$9=d; print}' """+test_report_path+"""/hardwareUtilization/CPUUSAGE_1.txt > """+test_report_path+"""/hardwareUtilization/CPUUSAGE.txt""")
    subprocess_cmd("""rm -rf """+test_report_path+"""/hardwareUtilization/CPUUSAGE_1.txt""")


def execute_preTestScript():
    pretest_exec_script = str(jmeter_config['customScripts']['preTestExecution']).lower()
    preTestExecutionNodes = str(jmeter_config['customScripts']['preTestExecutionNodes']).split(",")
    pretest_script = jmeter_config['customScripts']['preTestScript']
    if(pretest_exec_script) == "true":
        if(path.exists(path.join(project_path,"scripts", pretest_script))):
            for ip in  preTestExecutionNodes:
                logging.info(f'nodes are : {ip}')
                if lg_ip == ip :
                    pre_test_cmd = f'python3 {path.join(project_path,"scripts", pretest_script)} {project_path} {start_time}'
                    subprocess.Popen(pre_test_cmd, shell=True).wait()
        else:
            logging.info(f"PreTestScript.py doesnot exist")


def execute_postScript():
    posttest_exec_script = str(jmeter_config['customScripts']['postTestExecution']).lower()
    posttest_script = str(jmeter_config['customScripts']['postTestScript']).lower()
    if(posttest_exec_script) == "true":
        if(path.exists(path.join(project_path,"scripts", posttest_script))):
            post_test_cmd = f'python3 {path.join(project_path,"scripts", posttest_script)} {project_path} {start_time}'
            subprocess.Popen(post_test_cmd, shell=True).wait()
        else:
            logging.info(f"PostTestScript.py doesnot exist")


def find_perf_anomaly():
    status_file = f'{project_path}/results/build-status-report-data.txt'

    baseline_value = int(jmeter_config['DB']['baseline'])
    high_resp_count_query = f"select count(*) from {micro_service} where rawtable = 'table_{report_file}' and  "  \
                    f" p90tile > {baseline_value} ; "
    error_count_query = f"select count(*) from {micro_service} where rawtable = 'table_{report_file}' "  \
                    f" and error > 0 ; "
    hw_count_query = f"select count(*) from hardware_utilization  where raw_table = 'table_{report_file}' and  "  \
                    f" cpu_90th_per > 70 ; "
    cur = create_conn()
    cur.execute(high_resp_count_query)
    high_resp_count = cur.fetchone()
    cur.execute(error_count_query)
    error_count = cur.fetchone()
    cur.execute(hw_count_query)
    hw_count = cur.fetchone()

    with open(status_file, 'w') as f:
        f.write(f"{csv_file}\n")
        if (high_resp_count[0] + error_count[0] + hw_count[0]) > 0 :
            f.write("Failed\n\n")
        else:
            f.write("Passed\n\n")

        f.write("HighResponseTime\n")
        if high_resp_count[0] > 0:
            high_resp_query = f"select api_name , p90tile from {micro_service} where rawtable = 'table_{report_file}' " \
                              f" and p90tile > {baseline_value}"
            high_resp_api = "COPY ({0}) TO STDOUT WITH CSV HEADER".format(high_resp_query)
            cur.copy_expert(high_resp_api, f)
        else:
            f.write("Response time are in exceptable limits.\n")
        f.write("EndOfResponseTime\n\n")

        f.write("ObservedErrors\n")
        if error_count[0] > 0:
            error_query = f"select api_name , error from {micro_service} where rawtable = 'table_{report_file}' " \
                          f" and error > 0 "
            error_query_api = "COPY ({0}) TO STDOUT WITH CSV HEADER".format(error_query)
            cur.copy_expert(error_query_api, f)
        else:
            f.write("No Errors observed during the test.\n")
        f.write("EndOfObservedErrors\n\n")
        f.write("HardwareAnomaly\n")
        if hw_count[0] > 0 :
            hw_query = f"select hostname , cpu_90th_per from hardware_utilization  where raw_table = 'table_{report_file}' and  " \
                       f" cpu_90th_per > 70 "
            hw_query_nodes = "COPY ({0}) TO STDOUT WITH CSV HEADER".format(hw_query)
            cur.copy_expert(hw_query_nodes, f)
        else:
            f.write("Hardware utilization is in acceptable limits.\n")
        f.write("EndOfHardwareAnomaly\n")


with open(configfile) as f:
    jmeter_config = json.load(f)

project_path = jmeter_config['JmeterSetting']['mainSetting']['projectpath']
JMETER_HOME = jmeter_config['JmeterSetting']['mainSetting']['JMETER_HOME']
jmxfile_name = jmeter_config['JmeterSetting']['mainSetting']['jmxfile']
report_file_pre = jmeter_config['JmeterSetting']['mainSetting']['ReportFilePrefix']
lg_ip = subprocess.check_output("hostname -i", shell=True).decode().strip()
micro_service = jmeter_config['JmeterSetting']['mainSetting']['micro_service']
is_sanityTest = str(jmeter_config['JmeterSetting']['mainSetting']['sanityTest']).lower()
sanityTestDuration = float(jmeter_config['JmeterSetting']['mainSetting']['sanityTestDuration'])
waitTime = float(jmeter_config['JmeterSetting']['mainSetting']['waitTime'])
JMX_OPT = jmeter_config['JAVA_OPTIONS']['JMX_OPT']
db_host = jmeter_config['DB']['host']
db_port = jmeter_config['DB']['port']
db_user = jmeter_config['DB']['user']
database = jmeter_config['DB']['database']

result_dir = f'mkdir -p -m 755 {path.join(project_path, "results/backup")}'
subprocess.call(result_dir, shell=True)

logs_dir = f'mkdir -p -m 755 {path.join(project_path, "logs/backup")}'
subprocess.call(logs_dir, shell=True)

GC_dir = f'mkdir -p -m 755 {path.join(project_path, "logs/GC")}'
subprocess.call(GC_dir, shell=True)

backup_logs_cmd = f'mv {path.join(project_path, "logs/*.log")} {path.join(project_path, "logs/backup/")}'
subprocess.call(backup_logs_cmd, shell=True, stderr=subprocess.DEVNULL)


logFormat = '[%(asctime)s] %(levelname)s @ line %(lineno)d: %(message)s'
logging.basicConfig(filename=path.join(project_path, "logs/loadtest.log"), format=logFormat, level=logging.INFO,
                    datefmt='%d-%b-%Y %H:%M:%S')
logging.info(f"-------Starting Performance Test Automation on {lg_ip} for {micro_service}-------")
logging.info(f"Project Path : {project_path}")
logging.info(f"Start Time :  {start_time}")
logging.info(f"Build Number :  {build_no}")

is_split_mode = str(jmeter_config['JmeterSetting']['SplitMode']['SplitMode']).lower()
getstats = str(jmeter_config['HardwareStats']['getstats']).lower()

total_run_time = int(jmeter_config['RunSetting']['totalRunDuration'])
rampup_time = int(jmeter_config['RunSetting']['HighestRampup'])

hours, minutes = map(int, start_time.split(':'))
test_time = datetime.combine(date.today(), datetime.min.time())
test_duration = 0.0
if is_split_mode == "true":
    test_time = test_time + timedelta(hours=hours, minutes=minutes)
else:
    test_time = datetime.now()
if is_sanityTest == "true":
    test_time = test_time + timedelta(seconds=(sanityTestDuration + waitTime))

loadtest_time = test_time.strftime('%H:%M')

peak_start_time = (test_time + timedelta(seconds=rampup_time)).strftime("%d-%b-%Y %H:%M:%S")
peak_end_time = (test_time + timedelta(seconds=total_run_time)).strftime("%d-%b-%Y %H:%M:%S")

peak_duration = int(total_run_time) - rampup_time

utc_test_time = test_time - timedelta(hours=5, minutes=30)
test_time = test_time.strftime("%d%b%Y_%H%M%S")
hw_time = utc_test_time.strftime('%H:%M')

if is_split_mode == "true":
    master_ip = str(jmeter_config['JmeterSetting']['SplitMode']['Master']).strip()
    slave_list = str(jmeter_config['JmeterSetting']['SplitMode']['SlaveList']).split(",")
    if master_ip == lg_ip:
        is_master = True
        #start_hardware_stats(loadtest_time)
        copy_to_slave()
    else:
        is_master = False
    logging.info(f"Master Node is :  {master_ip}")
    logging.info(f"Slave IP : {slave_list}")
else:
    is_master = True

jmeter_sh = path.join(JMETER_HOME, "bin/jmeter")
jmx_file = path.join(project_path, jmxfile_name)

housekeeping()

loadtest_properties = path.join(project_path, "config", "loadtest.properties")
with open(loadtest_properties, "w") as lt:
    for scriptSetting in jmeter_config['JmeterSetting']['Script']:
        lt.write(f"{scriptSetting}={str(jmeter_config['JmeterSetting']['Script'][scriptSetting])}\n")
    lt.write(f'projectpath={project_path}')
logging.info(f"Load test Properties file Created at : {loadtest_properties}\n")

jmeter_cmd = f'JVM_ARGS="{JMX_OPT}" && export JVM_ARGS && bash {jmeter_sh} -q {loadtest_properties} -n -t {jmx_file}'

logging.info(jmeter_cmd)

execute_preTestScript()

if is_sanityTest == "true":
    sanitytest_command = f'{jmeter_cmd} -j {path.join(project_path, "logs/", "jmeter_sanity.log")}'
    if is_split_mode == "true":
        sanity_status = 1
        schedule.every().day.at(start_time).do(execute_test, lt_command=sanitytest_command, name="SanityTest")
        while sanity_status != 0:
            schedule.run_pending()
            time.sleep(1)
    else:
        execute_test(sanitytest_command, "SanityTest")
        time.sleep(waitTime)

report_file = f'{report_file_pre}_{test_time}'.lower()

test_report_path = path.join(project_path, "results", report_file)
subprocess.run("mkdir -p -m 755 " + test_report_path, shell=True)
db_csv_file = path.join(test_report_path, report_file) + "_db.csv"
csv_file = path.join(project_path, "results", report_file) + ".csv"
json_file = path.join(test_report_path, report_file) + ".json"
GC_Log_opt = "-Xloggc:" + project_path + "/logs/GC/GC_" + test_time + ".log"
HeapDump_opt = "-XX:HeapDumpPath=" + project_path + "/logs/GC"

jtl_file = path.join(test_report_path, report_file) + ".jtl"
loadtest_command = f'{jmeter_cmd} -l {jtl_file} -j {path.join(project_path, "logs", "jmeter_loadtest.log")}'
if is_split_mode == "true":
    # print(f"Load test time is {loadtest_time}")
    schedule.every().day.at(loadtest_time).do(execute_test, lt_command=loadtest_command, name="loadTest")
    run_status = 1
    if is_master:
        if getstats == "true":
            start_hardware_stats(hw_time)
    while run_status != 0:
        schedule.run_pending()
        time.sleep(1)
else:
    if is_master:
        if getstats == "true":
            start_hardware_stats(hw_time)
    execute_test(loadtest_command, "loadTest")

upload_jtl()
if is_master:
    process_report()
    if getstats == "true":
        get_hardware_stats()
        cloudwatch_metrics()
        consolidate_hw_files()
        upload_hardware_data_psql()
    execute_postScript()
    find_perf_anomaly()

    if 1 == 0:
        build_status = open(f'{project_path}/scripts/build-status-report-data.txt','w')
        build_status.write(f"{csv_file}\n")
        build_status.write("Passed\n")
        build_status.write("HighResponseTime\n")
        build_status.write("api_name | p90tile_\n")
        build_status.write("-----------------------------------------+---------\n")
        build_status.write("API_1 | 100\n")
        build_status.write("API_2 | 50\n")
        build_status.write("EndOfResponseTime\n")
        build_status.write("ObservedErrors\n")
        build_status.write("api_name | Error%_\n")
        build_status.write("-----------------------------------------+---------\n")
        build_status.write("API_1 | 20\n")
        build_status.write("API_2 | 30\n")
        build_status.write("EndOfObservedErrors\n")
        build_status.write("HardwareAnomaly\n")
        build_status.write("nodes | CPU utilization (90% tile)_\n")
        build_status.write("-----------------------------------------+---------\n")
        build_status.write("10.2.100.1 | 99%\n")
        build_status.write("EndOfHardwareAnomaly\n")
        build_status.close()

logging.info("-------Performance Test Automation Completed-------")
