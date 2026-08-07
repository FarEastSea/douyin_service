pipeline {
    agent any

    environment {
        REMOTE_HOST = credentials('douyin-remote-host')
        REMOTE_PORT = credentials('douyin-remote-port')
        REMOTE_USER = credentials('douyin-remote-user')
        REMOTE_SERVICE_DIR = credentials('douyin-remote-service-dir')
    }

    stages {
        stage('Deploy') {
            steps {
                sshagent(credentials: ['douyin-ssh-key']) {
                    sh '''
                        ssh -o StrictHostKeyChecking=accept-new -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" "
                            cd '$REMOTE_SERVICE_DIR' &&
                            chmod +x deploy.sh &&
                            ./deploy.sh
                        "
                    '''
                }
            }
        }
    }

    post {
        success {
            echo 'Deploy success.'
        }
        failure {
            echo 'Deploy failed.'
        }
    }
}
