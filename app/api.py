from flask import Blueprint,current_app,jsonify,request

from app.errors import SurveyProblem
from app.modules.ledger.service import archive
from app.modules.merge.release import release_merge
from app.modules.merge.service import issue
from app.modules.release.service import release
from app.pipeline import STAGES,run
from app.source import sources

survey = Blueprint('survey',__name__,url_prefix='/api')


def storage():
    return current_app.extensions['survey_archive']


def merge_storage():
    return current_app.extensions['merge_archive']


@survey.route('/survey/<stage>',methods=['GET','POST'])
def inspect(stage):
    if stage not in STAGES:
        raise SurveyProblem('NO_STAGE','找不到该统计阶段',404)
    if request.method=='GET' and 'run' in request.args:
        result=storage().read(request.args['run'])[2]
    else:
        body=request.get_json(silent=True) if request.method=='POST' else {'sample':request.args.get('sample','dirty')}
        result=run(sources(body),stage)
    return jsonify(result[stage])


@survey.get('/runs')
def list_runs():
    return jsonify(runs=storage().recent())


@survey.post('/runs')
def new_run():
    out=archive(storage(),sources(request.get_json(silent=True)))
    return jsonify(out), 200 if out['reused'] else 201


@survey.get('/runs/<key>')
def get_run(key):
    manifest,_,results=storage().read(key)
    return jsonify(id=key,manifest=manifest,balance=results['balance'],allocation=results['allocation'])


@survey.post('/runs/<key>/rerun')
def rerun(key):
    manifest,tables,_=storage().read(key)
    return jsonify(archive(storage(),tables,manifest))


@survey.get('/export/<key>')
def export(key):
    response=jsonify(release(*storage().read(key)))
    response.headers['Content-Disposition']=f'attachment; filename="survey-{key[:12]}.json"'
    return response


@survey.get('/merges')
def list_merges():
    return jsonify(merges=merge_storage().recent())


@survey.post('/merges')
def new_merge():
    body=request.get_json(silent=True)
    if not isinstance(body,dict) or set(body)!={'runs'} or not isinstance(body['runs'],list) \
            or len(body['runs'])!=2 or not all(isinstance(key,str) for key in body['runs']):
        raise SurveyProblem('BAD_INPUT','合档请求需提供两份站档编号 runs:[id_a,id_b]',422)
    out=issue(merge_storage(),storage(),*body['runs'])
    return jsonify(out), 200 if out['reused'] else 201


@survey.get('/merges/<merge_id>')
def get_merge(merge_id):
    document,results,seal,detail=merge_storage().read(merge_id)
    return jsonify(id=merge_id,document=document,stations=detail,seal=seal,
                   balance=results['balance'],allocation=results['allocation'])


@survey.get('/merges/<merge_id>/export')
def export_merge(merge_id):
    document,results,seal,_=merge_storage().read(merge_id)
    response=jsonify(release_merge(storage(),document,results,seal))
    response.headers['Content-Disposition']=f'attachment; filename="merge-{merge_id[:12]}.json"'
    return response
