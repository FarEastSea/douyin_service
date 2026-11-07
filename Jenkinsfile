pipeline {
    agent any

    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        // 最长存储维护任务排空 125 分钟，另留依赖准备、数据库恢复与验收时间。
        timeout(time: 240, unit: 'MINUTES')
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

# 只重试获取代码，不重试可能已经修改数据库或切换代码的整个发布流程。
for attempt in 1 2 3; do
    if git fetch --no-tags origin "refs/heads/$BRANCH"; then
        break
    else
        fetch_status=$?
    fi
    if [ "$attempt" -eq 3 ]; then
        echo "Deploy failed: unable to fetch the target branch after 3 attempts." >&2
        exit "$fetch_status"
    fi
    echo "Target branch fetch failed; retrying ($attempt/3)..." >&2
    sleep "$((attempt * 3))"
done
# FETCH_HEAD 对应本次成功拉取，不依赖可能过期的远程跟踪分支。
TARGET_SHA="$(git rev-parse --verify 'FETCH_HEAD^{commit}')"
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
            echo 'Deployment pipeline failed. Check the stage log and server deployment receipt to determine whether failure occurred before the code switch or required recovery.'
        }
    }
}
