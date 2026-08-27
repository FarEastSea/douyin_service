pipeline {
    agent any

    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        timeout(time: 30, unit: 'MINUTES')
        timestamps()
    }

    environment {
        REMOTE_HOST = credentials('douyin-remote-host')
        REMOTE_PORT = credentials('douyin-remote-port')
        REMOTE_USER = credentials('douyin-remote-user')
        REMOTE_SERVICE_DIR = credentials('douyin-remote-service-dir')
        REMOTE_PYTHON_ENV = credentials('douyin-remote-python-env')
        DEPLOY_BRANCH = 'main'
        TZ = 'Asia/Shanghai'
    }

    stages {
        stage('Preflight') {
            steps {
                sshagent(credentials: ['34fe54f4-a6fd-4899-aa9f-432143a8a2f5']) {
                    sh '''
                        set +x
                        set -eu
                        for value in "$REMOTE_HOST" "$REMOTE_PORT" "$REMOTE_USER" "$REMOTE_SERVICE_DIR" "$REMOTE_PYTHON_ENV"; do
                            test -n "$value"
                        done
                        ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new \
                            -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" 'printf "SSH preflight OK\\n"'
                    '''
                }
            }
        }

        stage('Deploy') {
            steps {
                sshagent(credentials: ['34fe54f4-a6fd-4899-aa9f-432143a8a2f5']) {
                    sh '''
                        set +x
                        set -eu
                        ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 \
                            -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new \
                            -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" \
                            bash -s -- "$REMOTE_SERVICE_DIR" "$DEPLOY_BRANCH" "$REMOTE_PYTHON_ENV" <<'REMOTE_SCRIPT'
set -Eeuo pipefail
SERVICE_DIR="$1"
BRANCH="$2"
PYTHON_ENV="$3"
cd "$SERVICE_DIR"

git fetch origin "$BRANCH"
TARGET_SHA="$(git rev-parse "origin/$BRANCH")"
TEMP_DEPLOY="$(mktemp "${TMPDIR:-/tmp}/douyin-deploy.XXXXXX")"
trap 'rm -f "$TEMP_DEPLOY"' EXIT
git show "${TARGET_SHA}:deploy.sh" > "$TEMP_DEPLOY"
chmod 700 "$TEMP_DEPLOY"

PROJECT_DIR_OVERRIDE="$SERVICE_DIR" \
DEPLOY_BRANCH="$BRANCH" \
DEPLOY_TARGET_SHA="$TARGET_SHA" \
REMOTE_PYTHON_ENV="$PYTHON_ENV" \
    "$TEMP_DEPLOY"
REMOTE_SCRIPT
                    '''
                }
            }
        }
    }

    post {
        success {
            echo 'BT Panel dependencies, root deployment, immediate restart, and smoke checks succeeded.'
        }
        failure {
            echo 'Deploy failed. Review the stage log; deploy.sh restores the previous root commit after a switch failure.'
        }
    }
}
