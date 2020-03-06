from flask import render_template, request, make_response, jsonify, Flask, flash, redirect, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, IntegerField, MultipleFileField, validators

import sys, traceback, os, shutil
import socket
import logging
import nbformat as nbf
from notebook.auth.security import passwd, passwd_check
from db_setup import init_db, db_session


# Connection to Scheduler
HOST = '127.0.0.1'
PORT = 65432

app = Flask(__name__, template_folder='temp')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////mnt/internal/queue.db'
app.secret_key = "warumbraucheichdich"
app.config['UPLOAD_FOLDER'] = '/mnt/data'
app.config['PYTHONFILE_FOLDER'] = '/mnt/internal'

db = SQLAlchemy(app)
init_db()

# Models
class Task(db.Model):
    __tablename__ = "tasks"
 
    id = db.Column(db.Integer, primary_key=True)
    owner = db.Column(db.String)
    task_type = db.Column(db.String)
    duration = db.Column(db.Integer)
    program = db.Column(db.String)
    status = db.Column(db.String)
    pwd = db.Column(db.String)
 
    def __repr__(self):
        return "%s - %10s - id: %s" % (self.owner, self.task_type, self.id)


# Forms
class TaskForm(FlaskForm):
    supported_tasks = [('python', 'Python file(s)'), ('jupyter_notebook', 'Jupyter Notebook'),
                       ('empty_notebook', 'Empty Notebook')]
    task_type = SelectField('Task', choices=supported_tasks)
    owner = StringField('Ersteller', validators=[validators.Optional()])
    duration = IntegerField('geschätzte Dauer in Minuten', validators=[validators.Optional()])
    files = MultipleFileField('Program / Module')
    main = StringField('Name of main if many files', validators=[validators.Optional()])


class PwdForm(FlaskForm):
    owner = StringField('User', validators=[validators.DataRequired()])
    old_pwd = StringField('Old password', validators=[validators.DataRequired()])
    new_pwd = StringField('New password', validators=[validators.DataRequired()])
    

@app.route("/")
def index():
    # Start Session
    session['status'] = True
    session['files'] = []
    # Update Scheduler
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, PORT))
        s.sendall(b'update')
        data = s.recv(1024)

    qry = db_session.query(Task)
    results = qry.all()

    taskList = [vars(task) for task in results]
    # Keine Nachkommas
    for task in taskList:
        task['duration'] = int(task['duration'])
            
    return render_template('index.html', taskList=taskList)


@app.route('/dropzone', methods=['POST'])
def handle_drop():
    """
    Handle uploads from the dropzone and save them in a folder for later use
    """
    session['files'] = []
    session['status'] = False
    for key, f in request.files.items():
        if key.startswith('file'):
            logging.info("%s uploaded over dropzone" % f.filename)
            i = int(key.split("[")[-1].strip("]"))
            f.save(os.path.join(app.config['PYTHONFILE_FOLDER'], secure_filename(f.filename)))
            session['files'] = session['files'] + [(secure_filename(f.filename), request.form['fullPath_%d' % i])]
    session['status'] = True
    return '', 204


@app.route("/addtask", methods=["GET", "POST"])
def add_task():
    """
    Neue Aufgabe zur Warteschlange hinzufügen
    """
    form = TaskForm()
        
    if form.validate_on_submit():
        # save to queue
        task = Task()
        task.owner = secure_filename(form.owner.data) or "dfki"
        task.task_type = form.task_type.data
        task.duration = form.duration.data or 0
        task.status = 'Ready'

        # Ugly but finds next id
        qry = db_session.query(Task)
        results = qry.all()
        logging.info([vars(task) for task in results])
        task.id = max([int(vars(task)['id']) for task in results], default=0) + 1
        logging.info(task.id)
        
        # Save Script in folder of User
        directory = os.path.join(app.config['PYTHONFILE_FOLDER'], task.owner)
        # init standard pwd if new
        if not os.path.exists(directory):
            logging.info("Make Account for %s" % task.owner)
            os.makedirs(directory)
            with open(os.path.join(directory, "pwd"), "w+") as pwd:
                pwd.write(passwd(task.owner))
        with open(os.path.join(directory, "pwd"), "r") as pwd:
            task.pwd = pwd.read()

        # Place in sub folder of id
        directory = os.path.join(directory, str(task.id))
        if not os.path.exists(directory):
            os.makedirs(directory)
        else:
            # Delete previous files
            for f in os.listdir(directory):
                file_path = os.path.join(directory, f)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    flash('Failed to delete %s. Reason: %s - This should never happen!' % (file_path, e))
                    return redirect("/addtask")

        # Create empty notebook if none given
        if task.task_type == 'empty_notebook':
            nb = nbf.v4.new_notebook()
            nbf.write(nb, os.path.join(directory, "test.ipynb"))
            task.program = "test.ipynb"
            found = True

        # Uploaded Files
        else:
            # Try to find main
            task.program = form.main.data if form.main.data.endswith('.py') else form.main.data + ".py"
            found = False
            last = None

            if session['status'] is False:
                flash("Uploaded Files were not ready")
                return redirect("/addtask")
            elif len(session['files']) == 0:
                flash("Select a file")
                return redirect("/addtask")
            else:

                # Get rid of folder where files were in by stepping into the common prefix
                prefix = os.path.commonpath([path for _, path in session['files']])\
                    if len(session['files']) >= 2 else ""

                for file, fullPath in session['files']:
                    relpath = os.path.relpath(fullPath, prefix)

                    last = relpath
                    if file == task.program or relpath == task.program:
                        found = True
                        task.program = relpath

                    try:
                        # Move to correct location
                        loc = os.path.join(directory, os.path.dirname(relpath))
                        if not os.path.exists(loc):
                            os.makedirs(loc)
                        os.replace(os.path.join(app.config['PYTHONFILE_FOLDER'], file),
                                   os.path.join(directory, relpath))
                    # When no file provided
                    except IsADirectoryError:
                        flash("Select a file")
                        return redirect("/addtask")

        # Use last file uploaded if no main is found
        if not found:
            task.program = last

        # Convert only main to a notebook if not already
        if task.task_type == 'python':
            main_path = os.path.join(directory, task.program)
            nb = nbf.v4.new_notebook()

            with open(main_path) as f:
                code = f.read()

            nb.cells.append(nbf.v4.new_code_cell(code))
            nbf.write(nb, main_path.replace('.py', '.ipynb'))
            os.remove(main_path)
            task.program = task.program.replace('.py', '.ipynb')

        db_session.add(task)
        db_session.commit()

        # Update Scheduler
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((HOST, PORT))
            s.sendall(b'update')
            data = s.recv(1024)

        logging.info('Received status: %s' % data.decode('utf-8'))
        
        return redirect('/')
 
    return render_template('addtask.html', form=form)


@app.route("/changepwd", methods=["GET", "POST"])
def change_pwd():
    """
    Change the standard password
    """
    form = PwdForm()

    if form.validate_on_submit():

        owner = secure_filename(form.owner.data)
        old_pwd = secure_filename(form.old_pwd.data)
        new_pwd = secure_filename(form.new_pwd.data)

        pwd_path = os.path.join(app.config['PYTHONFILE_FOLDER'], secure_filename(owner), "pwd")
        # Check if it is a valid user
        if not os.path.exists(pwd_path):
            flash("User %s hasn't created a task yet" % owner)
            logging.info("No User with name %s" % owner)
            return redirect("/changepwd")

        saved_pwd = open(pwd_path, 'r').read()
        with open(pwd_path, "w") as pwd:
            # Change password
            if passwd_check(saved_pwd, old_pwd):
                pwd.write(passwd(new_pwd))
                logging.info("Password changed for user %s" % owner)
                return redirect('/')
            pwd.write(saved_pwd)
            flash("Old Password is wrong")

    return render_template('change_pwd.html', form=form)
    

@app.route("/upload", methods=["GET", "POST"])
def upload_dataset():
    """
    Upload a dataset to the database and show a fancy loading animation
    """
    if request.method == "POST":

        file = request.files["file"]

        filename = secure_filename(file.filename)
        logging.info("Uploaded Dataset: %s"%filename)
        try:
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        except:
            traceback.print_exc(file=sys.stderr)

        res = make_response(jsonify({"message": "File uploaded"}), 200)

        return res

    return render_template("upload.html")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(debug=True, host='0.0.0.0')

